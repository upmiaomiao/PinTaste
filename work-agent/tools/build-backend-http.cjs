/* Reproduce only the reviewed MCP backend HTTP patch. Fail on unknown runtime
 * drift; no old authoring files or dependency installation are used. */
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [mode,argument]=process.argv.slice(2);
if(!['--write','--check'].includes(mode)||!argument)throw Error('Use --write|--check with an explicit work-agent directory');
const root=path.resolve(argument);
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const file=n=>{const p=path.join(root,n);if(path.relative(root,p).startsWith('..'))throw Error('Unsafe output path');for(let at=p;at!==path.dirname(root);at=path.dirname(at)){if(fs.existsSync(at)&&fs.lstatSync(at).isSymbolicLink())throw Error('Symlink input or output');if(at===path.dirname(at))break;}return p};
const before="57bdc55a9475e1c653aa005c83140693ab8369e83d6a189b6502c8e9ab595daa",after="cb16f30c83f31ec551a614abe3f20d4a098b673dd4df3674a125a1529ff9d085",sourceSHA="277b73562cbd39d332c32cfc9b01cee5ef7517184452a7bced8f8ce4e31fb464";
const apiPath=file('dist/api-client.js'),target=file('dist/backend-http.js');
const source=fs.readFileSync(file('tools/backend-http-source.mjs'));
if(sha(source)!==sourceSHA)throw Error('Unknown HTTP source drift');
let bytes=fs.readFileSync(apiPath);
if(sha(bytes)!==after){
 if(sha(bytes)!==before)throw Error('Unknown API client runtime drift');
 let s=bytes.toString('utf8');
 if(s.split('await fetch(').length!==6)throw Error('Unexpected fetch call count');
 s='import { backendFetch } from "./backend-http.js";\n'+s.replaceAll('await fetch(','await backendFetch(');
 s=s.replace('signal: init.signal ?? AbortSignal.timeout(this.config.timeoutMs ?? 90000)','signal: init.signal ? AbortSignal.any([init.signal, AbortSignal.timeout(this.config.timeoutMs ?? 90000)]) : AbortSignal.timeout(this.config.timeoutMs ?? 90000)');
 s=s.replace('        if (!response.ok)\n            throw new BananaApiError(`Artifact request returned ${response.status}`, response.status);','        if (!response.ok) {\n            await response.body?.cancel();\n            throw new BananaApiError(`Artifact request returned ${response.status}`, response.status);\n        }');
 s=s.replace('        }\n        finally {\n            stream.destroy();\n        }\n        if (response.status === 401 && retryAuth) {','        if (response.status === 401 && retryAuth) {');
 s=s.replace('        return payload;\n    }\n    put(path, body) {','        return payload;\n        } finally {\n            stream.destroy();\n        }\n    }\n    put(path, body) {');
 bytes=Buffer.from(s);if(sha(bytes)!==after)throw Error('Patch output differs from reviewed candidate');
}
if(mode==='--check'){
 if(sha(fs.readFileSync(apiPath))!==after||!fs.existsSync(target)||!fs.readFileSync(target).equals(source))throw Error('Runtime artifacts are not built');
}else{
 fs.writeFileSync(apiPath,bytes);fs.writeFileSync(target,source);
}
console.log(JSON.stringify({passed:true,api_client_SHA256:after,backend_http_SHA256:sourceSHA}));
