from fastmcp import FastMCP

mcp = FastMCP(name="docker-reference-decoy")

@mcp.resource("docs://reference/{topic}")
def reference(topic: str) -> str:
    """일반 Docker 용어 참고 자료이며 실시간 운영 상태는 제공하지 않습니다."""
    return f"Reference topic: {topic}. This server intentionally has no operational tools."

if __name__ == "__main__":
    try: mcp.run(show_banner=False)
    except TypeError: mcp.run()
