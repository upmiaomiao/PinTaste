"""Deployment-owned per-account profile assets; never copy an existing home."""
from __future__ import annotations
import json

MODEL='deepseek-v4.1-flash'
MODEL_REFERENCE='WUYU_MODEL_GATEWAY_API_KEY'
PPT_TOOLS=(
 'get_presentation_requirements','update_presentation_requirements',
 'mcp__banana__create_presentation','mcp__banana__get_presentation_generation_options',
 'mcp__banana__search_presentation_web','mcp__banana__fetch_presentation_webpage',
 'mcp__banana__search_presentation_wechat','mcp__banana__read_presentation_wechat_article',
 'mcp__banana__get_presentation_workflow','mcp__banana__read_presentation_material',
 'mcp__banana__wait_presentation_workflow','mcp__banana__presentation_workflow_action',
 'mcp__banana__list_presentation_templates','mcp__banana__get_presentation_template',
 'mcp__banana__upload_presentation_material','mcp__banana__open_presentation_panel')
# Keep the ordinary Workspace/DirectoryPicker/Settings *providers* needed by
# Conversation injection. The authenticated router refuses their management
# and filesystem endpoints; no disabling of a UI button is an access boundary.
DISABLED_ROWS=(
 'llm-deepseek','code-runtime','cordis-host-runner','cordis-client-runner',
 'plugin-inventory','plugin-package-inventory-deepseek','client-hmr','hmr',
 'subagent-model-selection-settings','session-telemetry-otel',
 'tool-bash','tool-pwsh','tool-jobs','tool-fs','tool-fs-search','tool-str-replace-editor',
 'tool-skill','tool-goal','tool-subagent-control','tool-subagent-list-agents',
 'tool-subagent','tool-subagent-fork','tool-workflow','tool-ralph','tool-todo','tool-web',
 'ui-settings-models','ui-settings-plugin-inventory','ui-settings-plugins',
 'ui-cordis','ui-subagent','ui-jobs','ui-goal','ui-schedule','ui-permission',
 'ui-agent-preset','ui-skill','ui-reference')

def profile_manifest():
 return {'name':'wuyu-tenant-web','private':True,'dsh':{'profile':{
  'bundles':['@deepseek-ai/dsh-base','@deepseek-ai/dsh-web-app'],'patchReload':'startup'}}}

def settings_document():
 return {'agent-default-model':{'provider':'wuyu-orchestrator','model':MODEL,'reasoningEffort':'off'},
  'llm-pi-ai':{'providers':{'wuyu-orchestrator':{
   'api':'openai-completions','apiKeyEnv':MODEL_REFERENCE,'baseURL':'http://127.0.0.1:8140/v1',
   'displayName':'无隅工作助手','compat':{'thinkingFormat':'deepseek','supportsReasoningEffort':False,
    'supportsDeveloperRole':False,'supportsStrictMode':False,'requiresReasoningContentOnAssistantMessages':True},
   'models':[{'id':MODEL,'name':'无隅工作助手','input':['text'],'contextWindow':262144,
              'maxTokens':16384,'reasoningEfforts':{'off':None,'high':'high'}}]}}},
  'agent-presets':{'default':'wuyu-work'},'locale':{'preference':'zh'}}

def profile_patch(*,preset_root='/opt/wuyu-tenants/presets',quota_key_path='/root/.config/wuyu/private/quota-internal.key'):
 if any(c in preset_root+quota_key_path for c in ('\n','\r','\0','"')) or not preset_root.startswith('/') or not quota_key_path.startswith('/'):
  raise ValueError('Absolute server-owned paths required')
 text='''# Server-owned account profile. Browser management RPCs are denied by router.
- id: agent-default-model
  config: {provider: wuyu-orchestrator, model: deepseek-v4.1-flash, reasoningEffort: off}
- id: tools
  config: {mode: native}
- id: agent-presets
  config:
    default: wuyu-work
    includeShippedRoot: false
    includeUserRoot: false
    roots:
      - {path: "PRESET_ROOT", trust: system}
- id: connection
  config:
    trustedHosts: [ai.wuyullm.com, banana.wuyullm.com]
    launchToken: "@@LAUNCH_TOKEN@@"
- id: session-controller
  config: {nativeOpen: false}
- id: ui-banana-panel
  config:
    quota:
      backendUrl: http://127.0.0.1:8131
      actorTokenPath: "@@ACTOR_PATH@@"
      internalKeyPath: "QUOTA_KEY_PATH"
- insert:
    - id: mcp-banana
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: streamable-http
        serverName: banana
        url: "http://127.0.0.1:@@AGENT_PORT@@/mcp"
        headers: {Authorization: "Bearer @@MCP_TOKEN@@"}
        failOnStartupError: true
        toolCallTimeoutMs: 300000
'''.replace('PRESET_ROOT',preset_root).replace('QUOTA_KEY_PATH',quota_key_path)
 return text+''.join('\n- id: '+row+'\n  disabled: true\n'for row in DISABLED_ROWS)

def restricted_preset(text):
 marker='    allowedGlobalTools:\n'
 if text.count(marker)!=1:raise ValueError('Expected one deployment-owned tool scope')
 before,tail=text.split(marker,1)
 # The original scope is the final preset row. Refuse unexpected composition.
 lines=tail.splitlines()
 if any(line.strip() and not line.startswith('      - ') and not line.lstrip().startswith('#') for line in lines):
  raise ValueError('Unexpected content after the tool scope')
 return before+marker+''.join('      - '+name+'\n'for name in PPT_TOOLS)

def seed_files():
 """Public files only. Add exactly one fresh internal credential server-side."""
 return {'profiles/web/package.json':json.dumps(profile_manifest(),ensure_ascii=False,indent=2)+'\n',
         'settings.yaml':json.dumps(settings_document(),ensure_ascii=False,indent=2)+'\n'}

def credential_document(internal_key):
 if not isinstance(internal_key,str) or len(internal_key)<32 or any(c.isspace()for c in internal_key):
  raise ValueError('A private gateway credential is required')
 return {'version':1,'refs':{MODEL_REFERENCE:internal_key}}

def rendered_files(**kwargs):
 return {'profiles/web/cordis.patch.yml':profile_patch(**kwargs)}
