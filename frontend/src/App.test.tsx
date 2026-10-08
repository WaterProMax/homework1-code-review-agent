import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
vi.mock('@monaco-editor/react',()=>({default:({value}:{value:string})=><pre data-testid="source">{value}</pre>,DiffEditor:()=> <div>差异视图</div>}));
import App from './App';

const file={path:'app.py',size:56,available:true,reason:''};
const project={id:'project',name:'demo',root:'/demo',default_path:null,files:[file]};
const finding={finding_id:'F001',path:'app.py',line:1,end_line:1,title:'可变默认值',severity:'高',evidence:'[]',suggestion:'改用 None',source:'tool',confidence:'confirmed',status:'open',verification:'not_checked'};
const run={id:'run',created_at:'2026-10-08T10:00:00Z',status:'completed',paths:['app.py'],summary:'发现一个问题',findings:[finding],errors:{},sources:{'app.py':{path:'app.py',source:'def f(x=[]):\n    return x',version:'abc',size:27}},replies:{},events:[],elapsed_seconds:1,changes:[]};
let session={id:'session',project_id:'project',runs:[] as string[],conversation:[],history:[] as unknown[]};
const mockFetch=vi.fn(async(url:string,options?:RequestInit)=>{
  let data:unknown;
  if(url==='/api/health') data={model_configured:true,demo_path:'/demo'};
  else if(url==='/api/projects') data=project;
  else if(url==='/api/sessions') data=session;
  else if(url==='/api/sessions/session') data=session;
  else if(url.startsWith('/api/projects/project/source')) data=run.sources['app.py'];
  else if(url==='/api/sessions/session/jobs') data={id:'job',kind:'review',status:'queued',events:[],completed_files:0,total_files:1,result:null,error:null};
  else if(url.startsWith('/api/jobs/job')) {session={...session,runs:['run'],history:[run]}; data={id:'job',kind:'review',status:'completed',events:[{seq:1,time:'2026-10-08T10:00:00Z',kind:'tool',text:'parse_python'}],completed_files:1,total_files:1,result:{run_id:'run'},error:null};}
  else if(url==='/api/runs/run') data=run;
  else throw new Error('unexpected request '+url+' '+options?.method);
  return {ok:true,json:async()=>data};
});
beforeEach(()=>{localStorage.clear();session={id:'session',project_id:'project',runs:[],conversation:[],history:[]};vi.stubGlobal('fetch',mockFetch);});
afterEach(()=>{cleanup();vi.unstubAllGlobals();mockFetch.mockClear();});
describe('workbench',()=>{
  it('shows an actionable empty state and disables review before loading',async()=>{
    render(<App/>);
    expect(screen.getByRole('button',{name:'开始审查'})).toBeDisabled();
    expect(screen.getByLabelText('项目路径')).toBeVisible();
    expect(screen.getByText('把代码看得更清楚')).toBeVisible();
    await waitFor(()=>expect(screen.getByText('模型已配置')).toBeVisible());
  });
  it('loads a project, runs a job, displays evidence and report links',async()=>{
    const user=userEvent.setup();render(<App/>);
    await user.type(screen.getByLabelText('项目路径'),'/demo');
    await user.click(screen.getByRole('button',{name:'加载项目'}));
    await waitFor(()=>expect(screen.getByRole('button',{name:'开始审查'})).toBeEnabled());
    expect(screen.getByLabelText('选择 app.py')).toBeChecked();
    await user.click(screen.getByRole('button',{name:'开始审查'}));
    expect(await screen.findByText('可变默认值')).toBeVisible();
    expect(screen.getByText('已证实')).toBeVisible();
    expect(screen.getByRole('link',{name:'导出 Markdown 报告'})).toHaveAttribute('href','/api/runs/run/report?format=md');
    expect(screen.getByTestId('source')).toHaveTextContent('def f(x=[])');
    expect(localStorage.getItem('review-job')).toBeNull();
  });
  it('shows connection failures without claiming successful results',async()=>{
    mockFetch.mockRejectedValueOnce(new Error('offline'));render(<App/>);
    expect(await screen.findByRole('alert')).toHaveTextContent('无法连接本地服务');
    expect(screen.getByRole('button',{name:'开始审查'})).toBeDisabled();
  });
});
