"""Bounded config-file access, anchored to directory FDs; never reads .env for the model."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

from .sanitize import redact


MAX_BYTES = 65536
CONFIG_NAME = re.compile(r"(?:Dockerfile(?:\.[\w.-]+)?|\.dockerignore|(?:docker-)?compose(?:\.[\w.-]+)?\.ya?ml)\Z")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "data", ".docker-ops-backups"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ProjectFiles:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def relative(self, value: str) -> str:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise ValueError("invalid project-relative config path")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not CONFIG_NAME.fullmatch(path.name):
            raise ValueError("only project-relative Dockerfile/Compose/.dockerignore paths are allowed")
        if any(part.startswith(".") for part in path.parts[:-1]):
            raise ValueError("hidden directories are not config sources")
        return path.as_posix()

    @contextmanager
    def parent(self, value: str):
        path = PurePosixPath(self.relative(value))
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise RuntimeError("secure config access requires POSIX dir_fd/O_NOFOLLOW support")
        descriptors = [os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
        try:
            for part in path.parts[:-1]:
                descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1]))
            yield descriptors[-1], path.name
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def raw(self, value: str) -> bytes | None:
        with self.parent(value) as (parent, name):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                return None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                    raise ValueError("config must be a single-link regular file <= 64 KiB")
                with os.fdopen(os.dup(fd), "rb") as stream:
                    content = stream.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise ValueError("config exceeds 64 KiB")
                content.decode("utf-8")
                return content
            finally:
                os.close(fd)

    def read(self, value: str) -> dict:
        content = self.raw(value)
        if content is None:
            return {"path": value, "ok": False, "found": False, "sha256": None}
        original = content.decode("utf-8")
        text = redact(original, MAX_BYTES)
        return {"path": value, "ok": True, "found": True, "size": len(content), "sha256": sha(content),
                "content": text, "redacted": text != original, "symlink": False}

    def replace(self, value: str, content: bytes | None) -> None:
        """Atomic leaf replacement through an already anchored parent; existing symlinks refused."""
        if content is not None and len(content) > MAX_BYTES:
            raise ValueError("replacement exceeds 64 KiB")
        with self.parent(value) as (parent, name):
            try:
                previous = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(previous.st_mode) or previous.st_nlink != 1:
                    raise ValueError("refusing symlink, hardlink or non-regular config")
                mode = stat.S_IMODE(previous.st_mode)
            except FileNotFoundError:
                mode = 0o644
            if content is None:
                try: os.unlink(name, dir_fd=parent)
                except FileNotFoundError: pass
                return
            temp = ".docker-ops-" + uuid.uuid4().hex
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content); stream.flush(); os.fsync(stream.fileno()); os.fchmod(stream.fileno(), mode)
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try: os.unlink(temp, dir_fd=parent)
                except FileNotFoundError: pass

    def inventory(self) -> dict:
        files, scanned, truncated = [], 0, False
        for directory, dirs, names in os.walk(self.root, followlinks=False):
            relative = Path(directory).relative_to(self.root)
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not (Path(directory) / d).is_symlink()) if len(relative.parts) < 6 else []
            scanned += len(names) + len(dirs)
            for name in sorted(names):
                if not CONFIG_NAME.fullmatch(name): continue
                path = (relative / name).as_posix()
                try:
                    item = self.read(path)
                    item.pop("content", None)
                    item["kind"] = "dockerignore" if name == ".dockerignore" else "dockerfile" if name.startswith("Dockerfile") else "compose"
                    files.append(item)
                except (OSError, ValueError, UnicodeError) as exc:
                    files.append({"path": path, "ok": False, "symlink": (self.root / path).is_symlink(), "error": type(exc).__name__})
            if scanned > 5000 or len(files) >= 100:
                truncated = True; break
        return {"ok": True, "files": files[:100], "truncated": truncated, "max_depth": 6}

    def active_compose(self) -> list[str]:
        explicit = os.getenv("COMPOSE_FILE")
        if explicit:
            paths = explicit.split(os.getenv("COMPOSE_PATH_SEPARATOR", os.pathsep))
            result = []
            for path in paths:
                full = Path(path) if Path(path).is_absolute() else self.root / path
                relative = full.relative_to(self.root).as_posix()
                self.relative(relative)
                if self.raw(relative) is None:
                    raise ValueError("active Compose file is missing")
                result.append(relative)
            return result
        base = next((p for p in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml") if (self.root / p).exists()), None)
        if not base: raise ValueError("no Compose config in pinned project")
        overrides = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml", "docker-compose.override.yml")
        override = next((p for p in overrides if (self.root / p).exists()), None)
        return [base] + ([override] if override else [])

    def compose(self) -> dict:
        paths = self.active_compose()
        return {"ok": True, "project_dir": str(self.root), "active_files": paths, "compose_file_env": os.getenv("COMPOSE_FILE"), "files": [self.read(p) for p in paths]}
