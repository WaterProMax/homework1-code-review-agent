import { useEffect, useRef } from 'react';
import { DiffEditor } from '@monaco-editor/react';
import type { editor } from 'monaco-editor';

/** Detach the diff model before disposing it, cancelling outstanding diff work. */
export default function SafeDiff({before,after}:{before:string;after:string}) {
  const ref=useRef<editor.IStandaloneDiffEditor|null>(null);
  useEffect(()=>()=>{
    const diff=ref.current;
    if(!diff)return;
    const models=diff.getModel();
    diff.setModel(null);
    models?.original.dispose();models?.modified.dispose();
    ref.current=null;
  },[]);
  return <DiffEditor language="python" original={before} modified={after} keepCurrentOriginalModel keepCurrentModifiedModel onMount={diff=>{ref.current=diff;}} options={{readOnly:true,minimap:{enabled:false},automaticLayout:true,fontSize:14,renderSideBySide:true}}/>;
}
