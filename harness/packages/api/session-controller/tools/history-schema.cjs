const fs=require('fs'),crypto=require('crypto'),path=require('path');
const expected={'typert.host.js': '4b37fba1ef9da804b7f0aa0e02fa1831765aef9da2b842df69db37744904cd08', 'typert.remote-client.js': 'c87438185cbedd210b2e0e636ab1a3fb0d8fae97df1ebd722c8688594a7f3ddd'};
const [input,output]=process.argv.slice(2),name=path.basename(input),source=fs.readFileSync(input,'utf8'),sha=x=>crypto.createHash('sha256').update(x).digest('hex');
if(sha(source)!==expected[name])throw Error('Published RPC metadata baseline changed');
const newline=source.includes('\r\n')?'\r\n':'\n',needle="session_list_parameter_0$schema = z.object({"+newline+"  'cursor': z.string().readonly().optional(),";
if(source.split(needle).length!==2)throw Error('Session list schema changed');
const result=source.replace(needle,"session_list_parameter_0$schema = z.object({"+newline+"  'excludeArchived': z.boolean().readonly().optional(),"+newline+"  'cursor': z.string().readonly().optional(),");
fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,result);console.log(JSON.stringify({before:sha(source),after:sha(result),field:'excludeArchived',other_RPC_definitions_preserved:result.replace("  'excludeArchived': z.boolean().readonly().optional(),"+newline,'')===source}));
