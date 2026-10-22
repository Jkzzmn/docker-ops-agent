import asyncio
import copy
import json
import threading
import time

import pytest

from backend.approvals import sign_approval
from backend.config_validation import validate_review, grounded_suggestions, snapshots, dependency_order_notes
from backend.execution_policy import align_answer
from backend.routing import classify_request
from backend import runtime_actions
from backend.graph import build_graph, Dependencies
from backend.approvals import ApprovalStore
from backend.state import fresh_state
from backend.config_agent import build_config_agent
from types import SimpleNamespace
from tests.test_config_control import control
from tests.model_responses import ScriptedModel
from tests.test_diagnostics import FixtureMCP, run_graph


@pytest.fixture
def runtime(control, monkeypatch):
    d = control.d
    d.APPROVAL_LOCK, d.USED_APPROVALS = threading.Lock(), {}
    original = d._run
    def run(args, timeout=15):
        result = original(args, timeout)
        if args[:2] == ['docker', 'compose'] and args[2] in {'start', 'stop', 'restart'}:
            for service in args[3:]: d.states[service]['State']['Status'] = 'exited' if args[2] == 'stop' else 'running'
        return result
    d._run = run
    d.check_service_health = lambda s: {'ok': True, 'service': s, 'state': d.states[s]['State']['Status'], 'health': ''}
    monkeypatch.setattr(runtime_actions, 'docker', d)
    return control


def signed(plan, identity='runtime-test'):
    return sign_approval('test-config-secret', identity, 'execute_runtime_action', {'plan': plan}, time.time()+60)


@pytest.mark.parametrize('operation', ['start', 'stop', 'restart'])
def test_existing_container_actions_preview_then_execute_once(runtime, operation):
    plan = runtime_actions.preview_runtime_action(operation, ['api'])['plan']
    assert runtime.d.calls == []
    assert plan['commands'] == [['docker', 'compose', operation, 'api']]
    result = runtime_actions.execute_runtime_action(plan, signed(plan))
    assert result['ok'] and result['lifecycle_verified']
    assert result['health_verified'] is (None if operation == 'stop' else False)
    assert runtime.d.calls == plan['commands']
    with pytest.raises(PermissionError): runtime_actions.execute_runtime_action(plan, signed(plan))
    assert len(runtime.d.calls) == 1


def test_runtime_target_file_identity_and_signature_guards(runtime):
    plan = runtime_actions.preview_runtime_action('start', ['api'])['plan']
    changed = copy.deepcopy(plan); changed['commands'][0] = ['docker', 'compose', 'up', '-d']
    with pytest.raises(PermissionError): runtime_actions.execute_runtime_action(changed, signed(plan))
    with pytest.raises(ValueError): runtime_actions.execute_runtime_action(changed, signed(changed))
    runtime.d.states['api']['Id'] = 'replacement'
    with pytest.raises(ValueError): runtime_actions.execute_runtime_action(plan, signed(plan))
    assert runtime.d.calls == []
    runtime.d.states['api']['Id'] = 'api-old'
    runtime.files.root.joinpath('Dockerfile').write_text('FROM another-image\n')
    with pytest.raises(ValueError): runtime_actions.execute_runtime_action(plan, signed(plan))
    assert runtime.d.calls == []


def test_runtime_refuses_new_creation_and_unknown_services(runtime):
    with pytest.raises(ValueError): runtime_actions.preview_runtime_action('up', ['api'])
    with pytest.raises(ValueError): runtime_actions.preview_runtime_action('start', ['outside'])
    with pytest.raises(ValueError): runtime_actions.preview_runtime_action('start', ['api','api'])
    runtime.d.states.pop('api')
    runtime.d._inspect = lambda s: (_ for _ in ()).throw(ValueError('absent'))
    with pytest.raises(ValueError, match='새 생성'): runtime_actions.preview_runtime_action('start', ['api'])
    assert runtime.d.calls == []


def test_runtime_timeout_consumes_approval_without_retry(runtime):
    plan=runtime_actions.preview_runtime_action('restart',['api'])['plan']
    def timeout(args,timeout=15):
        runtime.d.calls.append(args)
        raise TimeoutError('outcome uncertain')
    runtime.d._run=timeout
    with pytest.raises(TimeoutError): runtime_actions.execute_runtime_action(plan,signed(plan))
    with pytest.raises(PermissionError): runtime_actions.execute_runtime_action(plan,signed(plan))
    assert len(runtime.d.calls)==1


def test_mcp_runtime_mutation_is_one_attempt_even_when_caller_requests_more():
    from backend.mcp_client import DockerMCPClient
    class Session:
        calls=0
        async def call_tool(self,*a,**kw):
            self.calls+=1
            raise ConnectionError('uncertain result')
    client=DockerMCPClient({}); session=Session(); client.sessions['ops']=session
    with pytest.raises(ConnectionError): asyncio.run(client.tool('execute_runtime_action',{},attempts=3))
    assert session.calls==1


class RuntimeModel(ScriptedModel):
    async def json(self, prompt, **kwargs):
        if prompt.startswith('Classify intent'):
            q = json.loads(prompt.split('Input=',1)[1])['query']
            return {'route':'runtime', 'reason':'explicit start request', 'operation':'start', 'action_quote':q,
                    'needs_context':False, 'project_reference':'current', 'requested_project':None,
                    'target_scope':'project', 'target_cardinality':'all', 'service':None}
        return await super().json(prompt, **kwargs)


def test_start_request_never_runs_file_review_or_executes_before_approval(runtime):
    class RuntimeMCP(FixtureMCP):
        async def tool(self, name, args):
            self.calls.append((name,args))
            assert name == 'preview_runtime_action'
            return runtime_actions.preview_runtime_action(**args),0
    mcp=RuntimeMCP()
    approvals=ApprovalStore()
    try:
        graph=build_graph(Dependencies(RuntimeModel(),mcp,approvals,allowed_services=('api','db'),selected_target={'project':'test-project'}))
        result=asyncio.run(graph.ainvoke(fresh_state(query='도커 띄워줘',thread_id='runtime')))
    finally: approvals.close()
    assert result['route']=='runtime'
    assert result['approval']['tool']=='execute_runtime_action'
    assert result['approval']['args']['plan']['services']==['api','db']
    assert result['execution_status']=='waiting_for_user'
    assert all(name=='preview_runtime_action' for name,_ in mcp.calls)
    assert not any(e.get('node')=='configuration' for e in result['events'])
    assert runtime.d.calls==[]


def test_runtime_semantics_keep_project_boundary_and_require_grounded_action():
    decision=asyncio.run(classify_request(RuntimeModel(),'도커 띄워줘',[],allowed_services=('api','worker'),selected_target={'project':'buttercast'}))
    assert decision['route']=='runtime' and decision['runtime_operation']=='start'
    assert decision['routing_target']=={'scope':'project','service':None}
    class Bad(RuntimeModel):
        async def json(self,prompt,**kwargs):
            r=await super().json(prompt,**kwargs); r['action_quote']='사용자가 말하지 않은 시작'; return r
    assert asyncio.run(classify_request(Bad(),'상태만 보여줘',[],allowed_services=('api',)))['route']=='unavailable'


def file_evidence(content='FROM python:3.11\nRUN pip install -e .\n'):
    return [{'id':'E1','tool':'read_dockerfile','args':{'path':'Dockerfile'},'observation':{'path':'Dockerfile','content':content,'ok':True,'found':True,'sha256':'abc','redacted':False}}]


def suggestion(**values):
    return {'rule':'model_review','path':'Dockerfile','snippet':'RUN pip install -e .','text':'배포 이미지에서 editable 설치를 사용합니다. 개발 목적이 아니라면 일반 패키지 설치를 검토하세요. 속도 차이는 측정하지 않았습니다.','evidence_ids':['E1'],**values}


def test_freeform_review_requires_same_file_exact_unique_source_and_stays_non_mutating():
    evidence=file_evidence(); review={'findings':[suggestion()],'edits':[]}
    checked=validate_review(evidence,review)
    grounded=grounded_suggestions(checked,review)
    assert grounded[0]['line_start']==2 and grounded[0]['line_end']==2
    assert checked['claims']==[] and checked['edits']==[]
    for bad in [suggestion(snippet='RUN invented command'),suggestion(path='other/Dockerfile'),suggestion(evidence_ids=['E99'])]:
        r={'findings':[bad]}
        assert grounded_suggestions(validate_review(evidence,r),r)==[]
    duplicate=file_evidence('RUN pip install -e .\nRUN pip install -e .\n')
    assert grounded_suggestions(validate_review(duplicate,review),review)==[]


def test_existing_dependency_order_is_reported_as_observation_not_a_fake_defect():
    content='FROM python:3.11\nCOPY --chown=user pyproject.toml ./\nRUN pip install -e .\nCOPY --chown=user package ./package\n'
    notes=dependency_order_notes(snapshots(file_evidence(content)))
    assert len(notes)==1 and notes[0]['line_start']==3 and notes[0]['line_end']==4
    assert '이미 분리' in notes[0]['text']
    wrong_order='FROM python:3.11\nCOPY . /app\nRUN pip install .\n'
    assert dependency_order_notes(snapshots(file_evidence(wrong_order)))==[]


def test_query_result_mismatch_cannot_publish_an_unrelated_config_answer_or_approval():
    state={'route':'runtime','result_kind':'configuration_review','final':'Compose healthcheck가 없습니다','approval':{'tool':'apply_project_config_patch'}}
    guarded=align_answer(state)
    assert guarded['approval'] is None and guarded['answer_status']=='unavailable'
    assert '다른 종류' in guarded['final']


@pytest.mark.parametrize('accepted', [True,False])
def test_broader_review_advice_requires_independent_judge_and_never_grants_write(accepted):
    class ReviewModel:
        async def json(self,prompt,**kwargs):
            if prompt.startswith('Check configuration review suggestions'):
                assert kwargs['quality'] is True
                return {'accepted_ids':['A1'] if accepted else []}
            data=json.loads(prompt)
            file=next(e for e in data['evidence'] if e['tool']=='read_dockerfile' and e['args']['path']=='Dockerfile')
            return {'summary':'','findings':[suggestion(evidence_ids=[file['id']])],'edits':[]}
    class ReviewMCP:
        async def tool(self,name,args):
            if name=='read_compose_files': return {'ok':True,'files':[],'project_dir':'/test','active_files':['compose.yaml']},0
            if name=='validate_compose_config': return {'ok':True,'service_config':{'api':{'build':{'context':'.','dockerfile':'Dockerfile'}}}},0
            if name=='read_dockerfile':
                return file_evidence()[0]['observation'] if args['path']=='Dockerfile' else {'ok':True,'found':True,'content':'','sha256':'ignore'},0
            return {'ok':True},0
    deps=SimpleNamespace(llm=ReviewModel(),mcp=ReviewMCP(),allowed_services=('api',))
    result=asyncio.run(build_config_agent(deps).ainvoke(fresh_state(query='현재 Dockerfile 비효율을 검토해주세요',route='configuration',thread_id='review')))
    assert result['approval'] is None
    assert result['result_kind']=='configuration_review'
    assert result['config_metrics']['review_suggestions']==int(accepted)
    assert ('파일 근거 기반 검토 의견' in result['final']) is accepted
    assert ('editable 설치를 사용' in result['final']) is accepted
    assert 'Compose에 healthcheck' not in result['final']


def test_runtime_approval_handler_executes_exactly_once_without_requiring_healthcheck(runtime,monkeypatch):
    from backend import server
    from collections import Counter
    class MCP:
        approval_secret='test-config-secret'
        async def tool(self,name,args,attempts=3):
            assert name=='execute_runtime_action' and attempts==1
            return runtime_actions.execute_runtime_action(**args),0
    store=ApprovalStore()
    plan=runtime_actions.preview_runtime_action('start',['api'])['plan']
    action=store.create('test','execute_runtime_action',{'plan':plan},'explicit test approval')
    monkeypatch.setattr(server.app.state,'bundle',{'approvals':store,'mcp':MCP(),'settings':SimpleNamespace(recovery_wait_seconds=0),'metrics':Counter()},raising=False)
    try:
        result=asyncio.run(server._approve(action.id))
        assert result['type']=='RuntimeAction' and result['result']['ok']
        assert not result['result']['health_verified'] and result['mutation_calls']==1
        with pytest.raises(Exception): asyncio.run(server._approve(action.id))
        assert runtime.d.calls==plan['commands']
    finally:store.close()
