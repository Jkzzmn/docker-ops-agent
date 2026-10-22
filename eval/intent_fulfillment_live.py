"""Reported three-question live regression. Never approve any Docker operation."""
import asyncio
import json
from pathlib import Path
import time
import uuid
import httpx


async def main():
    thread = 'intent-fix-' + uuid.uuid4().hex
    path = Path(__file__).with_name('results') / (thread + '.jsonl')
    path.parent.mkdir(exist_ok=True)
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8100', timeout=90) as client:
        health = (await client.get('/health')).json()
        assert health['docker_target']['project'] == 'buttercast', 'Select the reported buttercast scope before running this regression.'
        headers = {'X-Docker-Target': health['instance_id']}
        with path.open('x') as out:
            for query, route in [('api 상태만 알려주세요','status'),('현재 도커파일에서 비효율을 야기하고 잇는 코드는 어떤건가요?','configuration'),('도커 띄워줘','runtime')]:
                started=time.monotonic()
                response=await client.post('/chat',headers=headers,json={'message':query,'thread_id':thread})
                response.raise_for_status(); body=response.json(); meta=body['metadata']
                checks={'route':meta['route']==route,'no_mutation':meta['mutation_calls']==0,'bounded':not meta['execution_budget']['stop_reason']}
                if route=='status': checks.update(concise=meta['tool_calls']==1, readable='중지 상태' in body['reply'])
                if route=='configuration':
                    checks.update(file_focused='Dockerfile' in body['reply'] and 'Compose에 healthcheck' not in body['reply'], source_review=meta['config_metrics'].get('review_comments',0)>0 or 'pip 설치 단계(' in body['reply'])
                if route=='runtime':
                    action=body.get('approval') or {}
                    checks.update(approval=action.get('tool')=='execute_runtime_action', operation=meta.get('runtime_operation')=='start', no_file_review=not any(e.get('node')=='config_collect' for e in body['events']))
                    if action:
                        cancelled=await client.post('/actions/'+action['id']+'/reject',headers=headers)
                        assert cancelled.is_success, 'test approval cleanup failed'
                row={'query':query,'seconds':round(time.monotonic()-started,2),'checks':checks,'response':body}
                out.write(json.dumps(row,ensure_ascii=False)+'\n'); out.flush()
                print(json.dumps({k:v for k,v in row.items() if k!='response'}|{'models':meta['llm_usage']['model_calls'],'tools':meta['tool_calls']},ensure_ascii=False),flush=True)
                assert all(checks.values()),checks
    print(path)


if __name__=='__main__': asyncio.run(main())
