import React from 'react';
import ReactDOM from 'react-dom/client';
import { loader } from '@monaco-editor/react';
import * as monaco from 'monaco-editor';
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker';
import App from './App';
import './style.css';
// Bundle the editor locally: classroom demos do not depend on a CDN.
self.MonacoEnvironment = { getWorker: () => new EditorWorker() };
loader.config({monaco});
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
