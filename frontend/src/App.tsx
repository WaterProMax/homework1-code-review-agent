import { useEffect, useRef, useState } from 'react';
import Editor from '@monaco-editor/react';
import SafeDiff from './SafeDiff';
import type { editor } from 'monaco-editor';
import Markdown from 'react-markdown';
import { Code2, FolderOpen, FileCode2, Play, History, ChevronRight, Check, AlertTriangle, RefreshCw, Download, X, MessageSquare, Sparkles, Terminal, Copy, ArrowRight, LoaderCircle, PanelLeft, Search, ShieldCheck } from 'lucide-react';
import { api, downloadText } from './api';
import type { Project, FileItem, Session, Run, Finding, Job, Source, Preview, Event } from './types';

const done = new Set(['completed','partial','failed','interrupted']);
const statusLabel: Record<string,string> = {queued:'准备中',running:'审查中',completed:'已完成',partial:'部分完成',failed:'失败',interrupted:'已中断'};
const verificationLabel: Record<string,string> = {new:'新增',still_present:'仍存在',inconclusive:'待确认',resolved:'已解决',not_checked:'待处理'};
const byteLabel = (size: number) => size < 1024 ? `${size} B` : `${(size/1024).toFixed(1)} KB`;

export default function App() {
  const [path,setPath] = useState(localStorage.getItem('review-path') || '');
  const [project,setProject] = useState<Project|null>(null);
  const [session,setSession] = useState<Session|null>(null);
  const [files,setFiles] = useState<FileItem[]>([]);
  const [selected,setSelected] = useState<string[]>([]);
  const [activePath,setActivePath] = useState('');
  const [source,setSource] = useState<Source|null>(null);
  const [run,setRun] = useState<Run|null>(null);
  const [findingId,setFindingId] = useState('');
  const [instruction,setInstruction] = useState('关注正确性、资源与异常处理');
  const [job,setJob] = useState<Job|null>(null);
  const [events,setEvents] = useState<Event[]>([]);
  const [error,setError] = useState('');
  const [loading,setLoading] = useState(false);
  const [modelConfigured,setModelConfigured] = useState<boolean|null>(null);
  const [demoPath,setDemoPath] = useState('');
  const [filter,setFilter] = useState('all');
  const [preview,setPreview] = useState<Preview|null>(null);
  const [previewStale,setPreviewStale] = useState(false);
  const [stale,setStale] = useState(false);
  const [question,setQuestion] = useState('');
  const [detailTab,setDetailTab] = useState('issue');
  const [view,setView] = useState('issues');
  const [mobileTab,setMobileTab] = useState('code');
  const [traceOpen,setTraceOpen] = useState(true);
  const [sourceLoading,setSourceLoading] = useState(false);
  const [notice,setNotice] = useState('');
  const editorRef = useRef<editor.IStandaloneCodeEditor|null>(null);
  const decorations = useRef<editor.IEditorDecorationsCollection|null>(null);
  const busy = loading || (!!job && !done.has(job.status));
  const finding = run?.findings.find(f => f.finding_id === findingId);
  const activeFindings = run?.findings.filter(f => f.status !== 'resolved') || [];
  const visibleFindings = (run?.findings || []).filter(f => filter==='all' || (filter==='resolved' ? f.status==='resolved' : filter==='pending' ? f.status!=='resolved' : f.severity===filter));

  async function refreshSession(id: string) {
    const result = await api<Session>(`/sessions/${id}`); setSession(result); return result;
  }
  async function openRun(id: string) {
    const result = await api<Run>(`/runs/${id}`);
    setRun(result); setPreview(null); setEvents(result.events); setFindingId(result.findings[0]?.finding_id || '');
    setActivePath(result.findings[0]?.path || result.paths[0] || '');
    return result;
  }
  async function loadProject(projectPath = path, restoring = false) {
    if (!projectPath.trim()) {setError('请输入本地 Python 文件或项目目录的路径');return;}
    setLoading(true);setError('');setPreview(null);
    try {
      const p = await api<Project>('/projects',{path:projectPath});
      const s = await api<Session>('/sessions',{project_id:p.id});
      const fileList = p.files;
      setProject(p);setFiles(fileList);setSession(s);setRun(null);setFindingId('');setEvents([]);setJob(null);setSource(null);
      setPath(projectPath);localStorage.setItem('review-path',projectPath);
      const defaultSelected = p.default_path ? [p.default_path] : fileList.filter(f=>f.available).slice(0,20).map(f=>f.path);
      setSelected(defaultSelected);setActivePath(defaultSelected[0] || '');
      const latest = await refreshSession(s.id);
      if (latest.runs.length) await openRun(latest.runs.at(-1)!);
      const storedJob = localStorage.getItem('review-job');
      if (restoring && storedJob) {
        try { const j = await api<Job>(`/jobs/${storedJob}`); if (!done.has(j.status)) {setJob(j);setEvents(j.events);} else localStorage.removeItem('review-job'); } catch {localStorage.removeItem('review-job');}
      }
    } catch (e) {setError((e as Error).message);} finally {setLoading(false);}
  }
  useEffect(()=>{
    api<{model_configured:boolean;demo_path:string}>('/health').then(h=>{setModelConfigured(h.model_configured);setDemoPath(h.demo_path);}).catch(()=>setError('无法连接本地服务，请确认后端已启动。'));
    const saved = localStorage.getItem('review-path'); if(saved) void loadProject(saved,true);
  },[]);

  useEffect(()=>{
    if (!project || !activePath) {setSource(null);return;}
    let alive = true;
    setSourceLoading(true);setStale(false);
    const captured = run?.sources?.[activePath];
    setSource(captured || null);
    const update = () => {
      api<Source>(`/projects/${project.id}/source?path=${encodeURIComponent(activePath)}`).then(current=>{
        if(!alive)return;
        if(captured) {setSource(captured);setStale(current.version!==captured.version);} else setSource(current);
        if(preview && preview.path===activePath) setPreviewStale(current.version!==preview.version);
      }).catch(e=>{if(alive){if(captured)setStale(true);else setError(e.message);}}).finally(()=>{if(alive)setSourceLoading(false);});
    };
    update();window.addEventListener('focus',update);
    return ()=>{alive=false;window.removeEventListener('focus',update);};
  },[project?.id,activePath,run?.id,preview?.version]);

  useEffect(()=>{
    if(!job || done.has(job.status))return;
    let active = true;let timer:ReturnType<typeof setTimeout>;
    let seq = Math.max(0,...events.map(e=>e.seq));
    const poll=async()=>{
      try {
        const next = await api<Job>(`/jobs/${job.id}?after_seq=${seq}`);
        if(!active)return;
        seq = Math.max(seq,...next.events.map(e=>e.seq));
        setEvents(old=>{const seen=new Set(old.map(e=>e.seq));return [...old,...next.events.filter(e=>!seen.has(e.seq))];});
        setJob(next);
        if(done.has(next.status)) {
          localStorage.removeItem('review-job');
          if(next.error)setError(next.error);
          if(next.result?.run_id) await openRun(next.result.run_id);
          if(next.result?.preview){setPreview(next.result.preview);setPreviewStale(false);}
          if(session)await refreshSession(session.id);
          if(next.status==='partial')setNotice('部分检查未完成，请查看报告中的未完成范围。');
          return;
        }
      } catch(e) {if(active)setError(`连接中断：${(e as Error).message}。正在重试。`);}
      if(active)timer=setTimeout(poll,1000);
    };
    timer=setTimeout(poll,300);
    return ()=>{active=false;clearTimeout(timer);};
  },[job?.id, job ? done.has(job.status) : true]);

  useEffect(()=>{
    const ed=editorRef.current;
    if(!ed)return;
    decorations.current?.clear();
    if(finding && finding.path===activePath && !finding.historical){
      decorations.current=ed.createDecorationsCollection([{range:{startLineNumber:finding.line,startColumn:1,endLineNumber:Math.max(finding.end_line,finding.line),endColumn:1},options:{isWholeLine:true,className:'finding-line',linesDecorationsClassName:'finding-gutter'}}]);
      ed.revealLineInCenter(finding.line);
    }
  },[findingId,source?.version,activePath]);

  async function startJob(kind: string, scoped = false) {
    if(!session || busy)return;
    if(kind==='review' && !selected.length){setError('请至少选择一个文件');return;}
    setLoading(true);setError('');setNotice('');setPreview(null);
    try {
      const result=await api<Job>(`/sessions/${session.id}/jobs`,{kind,paths:selected,instruction:kind==='chat'?question:instruction,run_id:run?.id || '',finding_id:kind==='review' || (kind==='recheck' && !scoped)? '': findingId});
      setJob(result);setEvents(result.events);setTraceOpen(true);localStorage.setItem('review-job',result.id);
      if(kind==='chat')setQuestion('');
    } catch(e){setError((e as Error).message);}finally{setLoading(false);}
  }
  function chooseFinding(f: Finding) {setFindingId(f.finding_id);setActivePath(f.path);setDetailTab('issue');setMobileTab('issues');}
  function toggleFile(file: FileItem) {setSelected(old=>old.includes(file.path)?old.filter(p=>p!==file.path):old.length<20?[...old,file.path]:old);}
  async function copy(text: string) {try{await navigator.clipboard.writeText(text);setNotice('已复制到剪贴板');}catch{setError('复制失败，请手动选择文本复制');}}
  const currentChat = session?.conversation.filter(c=>c.finding_id===findingId) || [];

  return <div className="app-shell">
    <aside className="rail"><div className="logo"><Code2 size={23}/></div><button className={view==='issues'?'rail-button active':'rail-button'} title="审查工作台" aria-label="审查工作台" onClick={()=>setView('issues')}><ShieldCheck size={21}/></button><button className={view==='history'?'rail-button active':'rail-button'} title="审查历史" aria-label="审查历史" onClick={()=>setView('history')}><History size={21}/></button><div className="rail-bottom"><span className="local-dot"/>LOCAL</div></aside>
    <div className="main-shell">
      <header className="header"><div><div className="brand">Review Studio <span>/ 代码审查</span></div><div className="header-caption">让每一个问题，都有代码依据。</div></div><div className="header-meta"><span className={`connection ${modelConfigured?'':'muted'}`}><span/>{modelConfigured?'模型已配置':modelConfigured===false?'静态检查可用':'连接中'}</span><span className="local-badge">本地工作台</span></div></header>
      <section className="project-bar"><div className="path-control"><FolderOpen size={18}/><input aria-label="项目路径" placeholder="输入本地项目目录或 .py 文件路径" value={path} onChange={e=>setPath(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!busy)void loadProject();}} disabled={busy}/><button className="secondary" disabled={busy} onClick={()=>void loadProject()}>{loading?<LoaderCircle className="spin" size={15}/>:null}加载项目</button></div><button className="demo-button" disabled={busy || !demoPath} onClick={()=>void loadProject(demoPath)}>试用演示项目 <ArrowRight size={14}/></button></section>
      {error && <div className="banner error" role="alert"><AlertTriangle size={16}/><span>{error}</span><button aria-label="关闭错误" onClick={()=>setError('')}><X size={15}/></button></div>}
      {notice && <div className="banner notice" role="status"><Check size={16}/><span>{notice}</span><button aria-label="关闭提示" onClick={()=>setNotice('')}><X size={15}/></button></div>}
      <section className="command-bar"><div className="scope-title"><span className="eyebrow">当前项目</span><strong>{project?.name || '尚未加载'}</strong>{project && <span className="selected-count">{selected.length} / {files.filter(f=>f.available).length} 个文件</span>}</div><div className="review-controls"><input aria-label="审查要求" value={instruction} onChange={e=>setInstruction(e.target.value)} placeholder="输入本次审查重点" disabled={busy}/><button className="primary" disabled={busy || !project || !selected.length} onClick={()=>void startJob('review')}>{busy?<LoaderCircle size={16} className="spin"/>:<Play size={15} fill="currentColor"/>}{busy?statusLabel[job?.status || 'queued']:'开始审查'}</button></div></section>
      <div className="mobile-tabs"><button onClick={()=>setMobileTab('files')} className={mobileTab==='files'?'active':''}>文件</button><button onClick={()=>setMobileTab('code')} className={mobileTab==='code'?'active':''}>源码</button><button onClick={()=>setMobileTab('issues')} className={mobileTab==='issues'?'active':''}>问题</button></div>
      <div className="workbench">
        <section className={`file-panel mobile-${mobileTab==='files'?'show':'hide'}`}><div className="panel-heading"><span><PanelLeft size={15}/>项目文件</span><span className="subtle">PYTHON</span></div>{project ? <><div className="tree-root"><FolderOpen size={16}/><span>{project.name}</span></div><div className="file-list">{files.length===0?<div className="empty-small">目录中没有可审查的 Python 文件。</div>:files.map(file=><div key={file.path} className={`file-row ${activePath===file.path?'active':''} ${!file.available?'unavailable':''}`} title={file.reason || file.path} style={{paddingLeft:12+Math.min(file.path.split('/').length-1,4)*10}}><input aria-label={`选择 ${file.path}`} type="checkbox" checked={selected.includes(file.path)} onChange={()=>toggleFile(file)} disabled={busy || !file.available || (!selected.includes(file.path)&&selected.length>=20)}/><button disabled={busy || !file.available} onClick={()=>{setActivePath(file.path);setMobileTab('code');}}><FileCode2 size={15}/><span>{file.path}</span></button><span className="file-size">{byteLabel(file.size)}</span></div>)}</div><div className="file-footer"><span>单次最多 20 个文件</span><span>80 KB / 文件 · 1 MB / 次</span></div></>:<div className="empty-files"><FolderOpen size={28}/><p>从一个项目开始</p><span>加载本地目录后，在这里选择要审查的文件。</span></div>}</section>
        <section className={`code-panel mobile-${mobileTab==='code'?'show':'hide'}`}><div className="code-tabs"><span><FileCode2 size={15}/>{activePath || '源码预览'}</span><span className="subtle">只读</span></div>{stale && <div className="stale-bar"><AlertTriangle size={14}/>文件已变化或移除，当前显示审查时的快照。<button disabled={busy} onClick={()=>void startJob('recheck')}>复查</button></div>}{source ? <><div className="source-meta"><span>{source.source.split('\n').length} 行</span><span>版本 {source.version.slice(0,8)}</span>{sourceLoading&&<LoaderCircle size={13} className="spin"/>}</div><div className="editor-wrap"><Editor path={activePath} language="python" value={source.source} theme="vs" options={{readOnly:true,minimap:{enabled:false},fontSize:14,fontFamily:'"SFMono-Regular", Consolas, monospace',lineHeight:24,padding:{top:16},scrollBeyondLastLine:false,automaticLayout:true,wordWrap:'off',renderLineHighlight:'none',overviewRulerLanes:0}} onMount={ed=>{editorRef.current=ed;if(finding&&finding.path===activePath&&!finding.historical){decorations.current=ed.createDecorationsCollection([{range:{startLineNumber:finding.line,startColumn:1,endLineNumber:Math.max(finding.end_line,finding.line),endColumn:1},options:{isWholeLine:true,className:'finding-line',linesDecorationsClassName:'finding-gutter'}}]);ed.revealLineInCenter(finding.line);}}} loading={<div className="editor-loading">正在加载源码编辑器…</div>}/></div></>:<div className="code-empty"><div className="code-empty-icon"><Code2 size={34}/></div><h1>把代码看得更清楚</h1><p>选择一个 Python 文件，查看源码、问题位置与修复建议。</p><div className="empty-steps"><span>01 加载项目</span><ChevronRight size={14}/><span>02 选择文件</span><ChevronRight size={14}/><span>03 开始审查</span></div>{demoPath&&<button className="secondary" disabled={busy} onClick={()=>void loadProject(demoPath)}>打开演示项目 <ArrowRight size={15}/></button>}</div>}</section>
        <section className={`issues-panel mobile-${mobileTab==='issues'?'show':'hide'}`}>
          <div className="panel-heading"><span>{view==='history'?<History size={16}/>:<Search size={16}/>} {view==='history'?'审查历史':'审查结果'}</span>{run&&<span className="run-state">{statusLabel[run.status]}</span>}</div>
          {view==='history'?<div className="history-list">{session?.history?.length ? session.history.map(h=><button key={h.id} className={`history-item ${run?.id===h.id?'active':''}`} disabled={busy} onClick={()=>{void openRun(h.id).catch(e=>setError(e.message));setView('issues');}}><div><strong>{new Date(h.created_at).toLocaleString('zh-CN')}</strong><span>{statusLabel[h.status]}</span></div><p>{h.summary}</p><span>{h.paths.length} 个文件 · {h.elapsed_seconds} 秒</span></button>):<div className="empty-small">还没有审查记录。完成一次审查后会自动保存在本地。</div>}</div>:<>
            <div className="result-overview"><div><strong>{activeFindings.length.toString().padStart(2,'0')}</strong><span>待处理问题</span></div><div className="risk-counts">{['高','中','低'].map(level=><span key={level}><i className={`risk-dot risk-${level}`}/>{level} <b>{activeFindings.filter(f=>f.severity===level).length}</b></span>)}</div></div>
            <div className="result-tools"><select aria-label="筛选问题" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">全部问题</option><option value="高">高严重程度</option><option value="中">中严重程度</option><option value="低">低严重程度</option><option value="pending">待处理</option><option value="resolved">已解决</option></select>{run&&<div><a href={`/api/runs/${run.id}/report?format=md`} title="导出 Markdown 报告" aria-label="导出 Markdown 报告"><Download size={15}/></a><a className="json-link" href={`/api/runs/${run.id}/report?format=json`}>JSON</a><button disabled={busy} title="复查本次范围" aria-label="复查本次范围" onClick={()=>void startJob('recheck')}><RefreshCw size={15}/></button></div>}</div>
            {run && Object.keys(run.errors).length>0 && <details className="incomplete"><summary><AlertTriangle size={13}/> {Object.keys(run.errors).length} 个文件存在未完成检查</summary>{Object.entries(run.errors).map(([p,e])=><p key={p}><b>{p}</b><br/>{e}</p>)}</details>}
            <div className="findings-list">{visibleFindings.length?visibleFindings.map(f=><button className={`finding-card ${findingId===f.finding_id?'selected':''}`} key={f.finding_id} onClick={()=>chooseFinding(f)}><div className="finding-top"><span className={`severity severity-${f.severity}`}>{f.severity}</span><span className="finding-id">{f.finding_id}</span><span className={`finding-status ${f.status==='resolved'?'resolved':''}`}>{f.status==='resolved'?'已解决':verificationLabel[f.verification] || '待处理'}</span></div><strong>{f.title}</strong><span className="finding-location"><FileCode2 size={12}/>{f.path}:{f.line}</span></button>):<div className="empty-results"><ShieldCheck size={28}/><p>{run?'当前筛选下没有问题':'等待第一次审查'}</p><span>{run?'查看其他筛选或完整报告。':'问题会按严重程度排列，并附上位置与证据。'}</span></div>}</div>
            {finding&&<div className="finding-detail"><div className="detail-tabs"><button className={detailTab==='issue'?'active':''} onClick={()=>setDetailTab('issue')}>问题详情</button><button className={detailTab==='chat'?'active':''} onClick={()=>setDetailTab('chat')}><MessageSquare size={13}/>追问</button></div>{detailTab==='issue'?<div className="detail-content"><div className="evidence-meta"><span>{finding.source==='tool'?'静态工具':'模型分析'}</span><span>{finding.confidence==='confirmed'?'已证实':'待确认'}</span></div><label>代码证据{finding.historical?' · 历史记录':''}</label><pre>{finding.evidence}</pre><label>改进建议</label><p>{finding.suggestion || '选择追问，进一步讨论修改方式。'}</p>{finding.verification_note&&<p className="verification-note">{finding.verification_note}</p>}<button className="recheck-issue" disabled={busy} onClick={()=>void startJob('recheck',true)}><RefreshCw size={13}/>复查此问题所在文件</button><button className="fix-button" disabled={busy||finding.status==='resolved'||!!finding.historical} onClick={()=>void startJob('fix')}><Sparkles size={15}/>查看修复预览<ArrowRight size={14}/></button></div>:<div className="chat-content"><div className="chat-messages">{currentChat.length?currentChat.map((c,i)=><div key={i}><div className="chat-question">{c.question}</div><div className="markdown"><Markdown>{c.reply}</Markdown></div></div>):<p className="subtle">针对 {finding.finding_id} 追问原因、影响或修改方式。</p>}</div><form onSubmit={e=>{e.preventDefault();void startJob('chat');}}><input aria-label="问题追问" value={question} onChange={e=>setQuestion(e.target.value)} placeholder="这个问题会在什么情况下发生？" disabled={busy}/><button className="primary" disabled={busy||!question.trim()} type="submit"><ArrowRight size={16}/></button></form></div>}</div>}
          </>}
        </section>
      </div>
      <section className={`trace ${traceOpen?'open':''}`}><button className="trace-heading" onClick={()=>setTraceOpen(!traceOpen)} aria-expanded={traceOpen}><span><Terminal size={15}/>执行过程<span className="trace-count">{events.length}</span></span><span>{job&&!done.has(job.status)?`${job.completed_files}/${job.total_files} 个文件` : run ? `${run.elapsed_seconds} 秒 · ${statusLabel[run.status]}`:'准备就绪'}<ChevronRight size={14} className={traceOpen?'rotated':''}/></span></button>{traceOpen&&<div className="trace-events" aria-live="polite">{events.length?events.map(e=><div className={`trace-event event-${e.kind}`} key={e.seq}><time>{new Date(e.time).toLocaleTimeString('zh-CN',{hour12:false})}</time><span className="event-kind">{e.kind}</span><span>{e.text}</span></div>):<div className="trace-placeholder">审查开始后，这里会显示实际的文件检查、工具调用与结果。</div>}</div>}</section>
      <footer className="footer"><span><span className="local-dot"/>源码在本地读取 · 修复预览不会修改文件</span><span>Python · Agent + AST</span></footer>
    </div>
    {preview&&<div className="modal-backdrop" onClick={()=>setPreview(null)}><section className="preview-modal" role="dialog" aria-modal="true" aria-label="修复预览" onClick={e=>e.stopPropagation()}><header><div><span className="eyebrow">修复预览 · {preview.finding_id}</span><h2>{preview.path}</h2></div><button aria-label="关闭修复预览" onClick={()=>setPreview(null)}><X size={20}/></button></header><div className="preview-explanation">{preview.explanation}<p>将建议应用到本地文件后，点击复查验证。此预览不代表业务行为已验证。</p></div>{previewStale&&<div className="banner error">文件已经变化，当前预览已过期。请关闭并复查。</div>}<div className="diff-wrap"><SafeDiff before={preview.before} after={preview.after}/></div><footer><span>版本 {preview.version.slice(0,8)}</span><div><button className="secondary" disabled={previewStale} onClick={()=>void copy(preview.after)}><Copy size={15}/>复制修改后代码</button><button className="secondary" disabled={previewStale} onClick={()=>downloadText(`${preview.finding_id}.patch`,preview.diff)}><Download size={15}/>下载补丁</button><button className="primary" disabled={busy} onClick={()=>{setPreview(null);void startJob('recheck');}}><RefreshCw size={15}/>已手动修改，复查</button></div></footer></section></div>}
  </div>;
}
