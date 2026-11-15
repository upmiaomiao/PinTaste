/**
 * Banana Slides deck panel, browser half. Three contributions share one
 * Session-scoped store:
 * - `tool.call.toolview` rows for the entry-point banana MCP tools extract the
 *   project handle and route it into the right details column.
 * - completed-Turn review cards expose real workflow decisions outside the
 *   collapsed Tool-process details.
 * - the `details` slot (right column) hosts the official project panel at a
 *   lower priority than the generic tool-details panel, so it replaces that
 *   column while a banana project is being worked on.
 *
 * The wire tool names follow the mcp-client convention
 * `mcp__<serverName>__<tool>`, with the banana server mounted as `banana`.
 */
import type { Context as ClientContext } from '@deepseek-ai/cordis'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
// Type-only: pulls the locale plugin's Context merge (ctx.locale).
import type {} from '@deepseek-ai/dsh-client-locale/client'
// Type-only: pulls the SlotRegistry service merge (ctx.slots).
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
// Type-only: pulls the layout service merge (ctx.layout.openDetails).
import type {} from '@deepseek-ai/dsh-client-ui-layout/client'
// Type-only: pulls the sidebar brand-slot types (sidebar.brand.name).
import type {} from '@deepseek-ai/dsh-client-ui-sidebar/client'
import type { BoundActions } from '@deepseek-ai/dsh-client-ui-slots'
import { BananaPanelRow } from './BananaPanelRow.tsx'
import { createReviewDecisionsDefinition, selectConversationDecisions } from './turn-review-decisions.ts'
import { BananaConversationDecisions } from './ResearchConversationPanel.tsx'
import { BANANA_TOOL_KEYS } from './tool-row.ts'
import { BananaDetailsPanel } from './BananaDetailsPanel.tsx'
import { WuyuBrandName, WuyuBrandMark } from './BrandName.tsx'
import { ComposerMaterials } from './ComposerMaterials.tsx'
import { ComposerMaterialCards } from './ComposerMaterialCards.tsx'
import { WorkUtilities, PlatformSettingsLink, PlatformHistoryLink } from './WorkUtilities.tsx'
import { createWorkShellStore, readWorkIdentity } from './work-shell.ts'
import { WorkTaskCards } from './WorkTaskCards.tsx'
import { WorkVoiceInput } from './WorkVoiceInput.tsx'
import { SearchModeToggle } from './SearchModeToggle.tsx'
import { PresentationTaskMode } from './PresentationTaskMode.tsx'
import { PresentationPreferences } from './PresentationPreferences.tsx'
import { PresentationPreferencesController, presentationPreferencesTransport } from './presentation-preferences.ts'
import { CurrentRequirementsToolRow } from './CurrentRequirementsToolRow.tsx'
import { SearchModeController, armResearchOnServer, type ArmedSearch } from './search-mode.ts'
import { TemplateDecisionSurface } from './TemplateDecisionPanel.tsx'
import { TemplateLibraryController, TemplateLibraryHttpClient } from './template-library.ts'
import { templatePanelRequest } from './template-panel-request.ts'
import { readTemplateWorkflowSnapshot } from './template-workflow-snapshot.ts'
import { bindWorkSessionLink } from './work-session-link.ts'
import { bindWorkUiLanguage } from './ui-language-bridge.ts'
import { isGenerationWorkSession } from './generation-settings.ts'
import { bindMaterialContext, materialIds } from './material-upload.ts'
import { createSnapshotStore } from '@deepseek-ai/dsh-client-store'
import { MaterialUploadQueue, initialUploadSnapshot } from './material-queue.ts'
import { readMaterialLimits, uploadBinaryMaterial, MaterialUploadError } from './material-http.ts'
import { connectBananaClient } from './banana-mcp.ts'
import { BANANA_MCP_URL, BANANA_MCP_TOKEN } from './config.ts'
import { createBananaPanelStore } from './store.ts'
import { readAccount, readQuotaLedger, redeemQuotaCode } from './account-quota.ts'
import { en, NS, zh, type BananaPanelKey } from './locales.ts'
declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap {
    /** The banana deck panel's copy. */
    bananaPanel: BananaPanelKey
  }
  interface SlotMap {
    /** Read-only neutral template browsing in the Session details header. */
    'wuyu.work.templates': { kind: 'single'; scope: 'session';owner:{mode:'recommendations'|'review'} }
  }
}
/** Required services: the slot registry, the locale registry, and the layout face. */
export const inject = ['slots', 'locale', 'layout', 'sessions', 'conversation', 'uiConversation']
/**
 * Client plugin body: register the banana deck dictionary, the transcript rows
 * that route the current project into the right column, and the right-column
 * deck panel itself.
 * @param ctx - client root context.
 */
export function apply(ctx: ClientContext): void {
  ctx.effect(() => ctx.locale.register(NS, { zh, en }), 'ui-banana-panel: dictionaries')
  ctx.effect(() => bindWorkUiLanguage(ctx.locale), 'ui-banana-panel: account UI language')
  const workShell = createWorkShellStore()
  ctx.effect(() => {
    const dispose = bindWorkSessionLink(ctx.sessions,new URL(location.href),url=>history.replaceState(null,'',url))
    if(new URL(location.href).searchParams.get('templates')==='review')ctx.layout.openDetails()
    return dispose
  }, 'ui-banana-panel: linked work record')
  ctx.slots.inject('conversation.work.account', () => ctx.slots.register({
    name: 'conversation.work.account', locale: NS, store: workShell,
    inject: () => ({ openWorkDetails: () => ctx.layout.openDetails(), navigateWork: (path: string) => location.assign(path), openWorkSession: (id: SessionId) => ctx.sessions.open(id), readIdentity: readWorkIdentity, readAccount, readQuotaLedger, redeemQuotaCode }),
  }, WorkUtilities))
  ctx.slots.inject('sidebar.settings', () => ctx.slots.register({
    name: 'sidebar.settings', priority: -10, locale: NS, store: workShell,
  }, PlatformSettingsLink))
  ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register({
    name: 'sidebar.footer.action', id: 'wuyu-work-history', locale: NS, store: workShell,
  }, PlatformHistoryLink))
  const store = createBananaPanelStore()
  const uploads = new Map<SessionId, { controller: MaterialUploadQueue; dispose: () => void }>()
  const templates = new Map<SessionId, { controller: TemplateLibraryController; dispose: () => void }>()
  const voices=new Map<SessionId,{active:boolean;dispose:()=>void}>()
  const searchModes = new Map<SessionId, { controller: SearchModeController; dispose(): void }>()
  const pageCounts = new Map<SessionId, PresentationPreferencesController>()
  const pageCount = (sessionId: SessionId) => {
    let controller = pageCounts.get(sessionId)
    if (!controller) {
      const actx = ctx.sessions.scope(sessionId)
      if (!actx) throw new Error('Page-count choices require a Session scope')
      const owned = new PresentationPreferencesController(presentationPreferencesTransport(sessionId))
      void owned.load().catch(()=>{})
      pageCounts.set(sessionId, owned)
      controller = owned
      actx.effect(() => () => { owned.dispose(); if (pageCounts.get(sessionId) === owned) pageCounts.delete(sessionId) }, 'wuyu-presentation-preferences.scope')
    }
    return controller
  }
  // Validate the count before the existing search modifier can arm research.
  const searchMode = (sessionId: SessionId) => {
    let entry = searchModes.get(sessionId)
    if (!entry) {
      const actx = ctx.sessions.scope(sessionId)
      if (!actx) throw new Error('Composer choices require a Session scope')
      const count = pageCount(sessionId)
      const controller = new SearchModeController(mode => armResearchOnServer(mode), () => {
        ctx.conversation.input.for(actx).notify('error', translate('search.armFailed'))
      })
      const remove = ctx.conversation.registerSubmissionModifier(sessionId, async (text: string) => {
        if (!isGenerationWorkSession(ctx.sessions.list.getSnapshot().byId[sessionId])) return text
        let message: string
        try { message = await count.submission(text, translate) }
        catch (error) {
          ctx.conversation.input.for(actx).notify('error', translate(error instanceof Error && error.message === 'PPT_PAGE_COUNT_INVALID' ? 'pageCount.invalid' : 'preferences.failed'))
          throw error
        }
        if(count.state.getSnapshot().status!=='ready'){controller.state.set({mode:'off'});return message}
        controller.state.set({mode:count.selected().search_mode})
        try { return await controller.submission(message) }
        catch { throw new Error(translate('search.armFailed')) }
      })
      let disposed = false
      const owned = { controller, dispose: () => { if (!disposed) { disposed = true; controller.dispose(); remove() } } }
      entry = owned
      searchModes.set(sessionId, owned)
      actx.effect(() => () => { owned.dispose(); if (searchModes.get(sessionId) === owned) searchModes.delete(sessionId) }, 'wuyu-search-mode.scope')
    }
    return entry.controller
  }
  ctx.effect(() => () => { for (const entry of searchModes.values()) entry.dispose(); searchModes.clear(); for (const controller of pageCounts.values()) controller.dispose(); pageCounts.clear() }, 'ui-banana-panel: composer choices')
  const selectTask = async (sessionId: SessionId, mode: 'create' | 'relayout', kind?: 'deck' | 'pages' | 'asset') => {
    await pageCount(sessionId).chooseTask(mode, kind)
    uploads.get(sessionId)?.controller.state.update(draft=>{draft.intakeError=null})
    if (mode === 'relayout') searchModes.get(sessionId)?.controller.state.set({mode:'off'})
  }
  const emptyPreferences = createSnapshotStore(new PresentationPreferencesController(async () => { throw new Error('Session required') }).state.getSnapshot())
  const preferenceInjected = (sessionId: SessionId) => {
    const controller = pageCount(sessionId)
    return { hooks:{preferences:controller.state}, editPreferences:(patch:Parameters<typeof controller.edit>[0])=>controller.edit(patch), chooseTask:(mode:'create'|'relayout',kind?:'deck'|'pages'|'asset')=>selectTask(sessionId,mode,kind), savePreferences:()=>controller.save(), reloadPreferences:()=>controller.load(), refreshPreferences:()=>controller.refresh() }
  }
  const translate = ctx.locale.bind(NS)
  ctx.uiConversation.events.register(createReviewDecisionsDefinition(location.href))
  ctx.effect(() => () => { for (const queue of uploads.values()) queue.dispose(); uploads.clear() }, 'ui-banana-panel: upload queues')
  ctx.effect(() => () => { for (const entry of templates.values()) entry.dispose(); templates.clear() }, 'ui-banana-panel: template choices')
  ctx.effect(()=>()=>{for(const voice of voices.values())voice.dispose();voices.clear()},'ui-banana-panel: voice admission')
  // Brand the sidebar with this PPT service's name.
  ctx.slots.inject('sidebar.brand.name', () =>
    ctx.slots.register({ name: 'sidebar.brand.name', locale: NS }, WuyuBrandName))
  ctx.slots.inject('sidebar.brand.mark', () => ctx.slots.register({name:'sidebar.brand.mark',priority:-5}, WuyuBrandMark))
  ctx.slots.inject('conversation.hero.brand.mark', () => ctx.slots.register({name:'conversation.hero.brand.mark',priority:-5}, WuyuBrandMark))
  ctx.slots.inject('conversation.hero.suggestions', () => ctx.slots.register({name:'conversation.hero.suggestions',locale:NS,inject:(sessionId:SessionId|undefined)=>sessionId===undefined?{hooks:{preferences:emptyPreferences},editPreferences:()=>{},chooseTask:async()=>{},savePreferences:async()=>{},reloadPreferences:async()=>{},refreshPreferences:async()=>{}}:preferenceInjected(sessionId)}, WorkTaskCards))
  const reviewInjected = (_sessionId: SessionId, actions: BoundActions<typeof store>) => ({
          openPanel: (handle: string, projectId?: string, target?: import('./preview-target.ts').PreviewTarget) => {
            actions.setProject(handle, projectId)
            actions.openPreview(target??{view:'slides'})
            ctx.layout.openDetails()
          },
          openTemplates:(handle:string,projectId?:string)=>{actions.setProject(handle,projectId);actions.showTemplates();ctx.layout.openDetails()},
          savePersonalTemplate:async(projectId:string,revision:number,title:string)=>{
            const response=await fetch('/agent-api/api/template-library/personal',{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:projectId,expected_revision:revision,title})})
            if(!response.ok)throw new Error('PERSONAL_TEMPLATE_SAVE_FAILED')
            const result:unknown=await response.json()
            if(!result||typeof result!=='object'||!('success'in result)||result.success!==true)throw new Error('PERSONAL_TEMPLATE_SAVE_FAILED')
          },
          writeReviewPrompt:async(text:string)=>{
            const actx=ctx.sessions.scope(_sessionId);if(!actx)throw new Error('Session unavailable')
            const input=ctx.conversation.input.for(actx),state=input.state.getSnapshot()
            if(state.phase!=='plain')throw new Error('Input busy')
            input.setDraft(state.draft?state.draft+'\n\n'+text:text)
          },
        })
  ctx.slots.inject('conversation.chat.turnTail', () => ctx.slots.register({
    name: 'conversation.chat.turnTail',
    priority: -10,
    select: selectConversationDecisions,
    locale: NS,
    store,
    inject: (sessionId: SessionId, actions: BoundActions<typeof store>) => reviewInjected(sessionId, actions),
  }, BananaConversationDecisions))
  ctx.slots.inject('tool.call.toolview', function* () {
    for(const key of ['get_presentation_requirements','update_presentation_requirements']){
      yield ctx.slots.register({name:'tool.call.toolview',key,locale:NS,inject:(sessionId:SessionId)=>({requirementsOperation:key==='get_presentation_requirements'?'read' as const:'update' as const,refreshRequirements:()=>pageCount(sessionId).refresh(),openRequirements:()=>{const controller=pageCount(sessionId),status=controller.state.getSnapshot().status;if(status!=='dirty'&&status!=='saving')void controller.load().catch(()=>{});ctx.layout.openDetails()}})},CurrentRequirementsToolRow)
    }
    for (const key of BANANA_TOOL_KEYS) {
      yield ctx.slots.register({
        name: 'tool.call.toolview',
        key,
        locale: NS,
        store,
        inject: (sessionId: SessionId, actions: BoundActions<typeof store>) => ({
          rememberProject:(handle:string,projectId?:string,mode?:'create'|'relayout')=>{actions.setProject(handle,projectId);void pageCount(sessionId).bindProject(handle,mode,true).catch(()=>{})},
          openPanel:reviewInjected(sessionId,actions).openPanel,
        }),
      }, BananaPanelRow)
    }
  })
  // A lower priority than the generic details panel (default 0) renders for a
  // `single` slot, so the deck panel owns the right column.
  ctx.slots.inject('details', () => ctx.slots.register({
    name: 'details',
    children: { 'wuyu.work.templates': { kind: 'single', scope: 'session' } },
    locale: NS,
    priority: -1,
    store,
    inject: (sessionId: SessionId) => ({
      ...preferenceInjected(sessionId),
      closeDetails: () => ctx.layout.closeDetails(),
      templateRequest:(handle:string,request:unknown)=>templatePanelRequest(handle,request,templateInjected(sessionId)),
      writeToConversation: async (text: string) => {
        const actx = ctx.sessions.scope(sessionId)
        if(!actx)throw new Error('Session unavailable')
        const input = ctx.conversation.input.for(actx)
        const draft = input.state.getSnapshot().draft
        input.setDraft(draft ? draft+'\n\n'+text : text)
      },
    }),
  }, BananaDetailsPanel))
  const templateInjected=(sessionId:SessionId)=>{
      let entry = templates.get(sessionId)
      if (!entry) {
        const actx = ctx.sessions.scope(sessionId)
        if (!actx) throw new Error('Template choices require a Session scope')
        const controller = new TemplateLibraryController({
          api:new TemplateLibraryHttpClient('/agent-api/api/template-library'), identity:async()=>{
            const response=await fetch('/agent-api/api/auth/me',{credentials:'same-origin',cache:'no-store'})
            if(!response.ok)throw new Error('TEMPLATE_IDENTITY_UNAVAILABLE')
            const body:unknown=await response.json()
            if(!body||typeof body!=='object'||!('data'in body)||!body.data||typeof body.data!=='object')throw new Error('TEMPLATE_IDENTITY_INVALID')
            const user=body.data as Record<string,unknown>,permissions=user.permissions
            const review=permissions&&typeof permissions==='object'&&'template_review'in permissions ? permissions.template_review : undefined
            return {admin:typeof review==='boolean'?review:user.role==='admin'}
          },
          workflow:async handle=>{
            const client = await connectBananaClient(BANANA_MCP_URL,BANANA_MCP_TOKEN)
            try {
              const result = await client.callTool('get_presentation_workflow',{project_handle:handle})
              return readTemplateWorkflowSnapshot(result,handle)
            } finally { client.dispose() }
          },
          persistence:{read:()=>sessionStorage.getItem('wuyu:template:v1:'+sessionId),write:value=>sessionStorage.setItem('wuyu:template:v1:'+sessionId,value)},
        })
        void controller.hydrateOnce()
        const removeModifier = ctx.conversation.registerSubmissionModifier(sessionId,text=>
          isGenerationWorkSession(ctx.sessions.list.getSnapshot().byId[sessionId]) ? controller.currentSubmission(text,translate('templateLibrary.summary')) : text)
        const owned = {controller,dispose:()=>{removeModifier();controller.dispose()}}
        entry=owned;templates.set(sessionId,owned)
        actx.effect(()=>()=>{owned.dispose();if(templates.get(sessionId)===owned)templates.delete(sessionId)},'wuyu-template-choice.scope')
      }
      const controller=entry.controller
      return {
        hooks:{templateLibrary:controller.state},
        loadTemplateLibrary:(filters?:import('./template-library.ts').TemplateFilters,tab?:import('./template-library.ts').TemplateLibrarySnapshot['tab'])=>controller.load(filters,tab),
        inspectTemplate:(id:string)=>controller.inspect(id),chooseTemplate:(card:import('./template-library.ts').NeutralTemplateCard|null)=>controller.choose(card),
        loadTemplateMaterials:(handle:string|null)=>controller.materials(handle),submitTemplate:(id:string)=>controller.submit(id),tagTemplate:(id:string)=>controller.tag(id),
        reviewTemplate:(id:string,decision:'approve'|'reject',reason:string,tags?:import('./template-library.ts').TemplateTags)=>controller.review(id,decision,reason,tags),
        loadRecommendations:(handle:string)=>controller.recommendations(handle),
        uploadPrivateTemplate:(handle:string,file:File)=>controller.uploadTemplate(handle,file),
        deleteMaterial:async(handle:string,materialId:string,revision:number,requestId:string)=>{
          const queue=uploads.get(sessionId)?.controller
          const operation=async()=>{const result=await controller.deleteMaterial(handle,materialId,revision,requestId);if(result.ok)await queue?.reconcileProjectMaterials(handle,result.materials);return result}
          try{return queue?await queue.manageProjectMaterials(operation):await operation()}
          catch{return {ok:false as const,code:'MATERIAL_IN_USE'}}
        },
        confirmTemplateChoice:async(handle:string,choice:{source_id:string}|{material_id:string})=>{
          const actx=ctx.sessions.scope(sessionId);if(!actx)return false
          const input=ctx.conversation.input.for(actx)
          const success=await controller.confirmChoice(handle,choice)
          const confirmed=controller.state.getSnapshot().confirmedChoice
          if(!success||!confirmed)return false
          try{await pageCount(sessionId).bindProject(handle,undefined,false,true)}
          catch{pageCount(sessionId).state.update(draft=>{draft.status='error'});input.notify('error',translate('current.templateSyncPending'))}
          const state=input.state.getSnapshot()
          if(state.phase!=='plain'){input.notify('info',translate('templateDecision.confirmed'));return true}
          const prompt=translate('templateDecision.confirmPrompt',{title:confirmed.title})
          // Never send a half-written user draft; keep the old behaviour in that case.
          if(state.draft&&state.draft.trim()){input.setDraft(state.draft+'\n\n'+prompt);input.notify('info',translate('templateDecision.confirmed'));return true}
          // op179: the click itself is the confirmation; tell the agent right away.
          input.setDraft(prompt)
          await new Promise(resolve=>setTimeout(resolve,60))
          const ready=input.state.getSnapshot()
          if(ready.phase==='plain'&&ready.draft.trim()===prompt.trim()){try{input.submit('queue');return true}catch{/* fall through */}}
          input.notify('info',translate('templateDecision.confirmed'))
          return true
        },
      }
  }
  // Attachments and preview navigation belong to the current conversation scope.
  const uploadInjected = (sessionId:SessionId, actions:BoundActions<typeof store>)=>{
      templateInjected(sessionId)
      let entry = uploads.get(sessionId)
      if (!entry) {
        const actx = ctx.sessions.scope(sessionId)
        if (!actx) throw new Error('Material uploads require a Session scope')
        const input = ctx.conversation.input.for(actx)
        const storageKey = 'wuyu-prepared-materials:' + sessionId
        const queue = new MaterialUploadQueue(createSnapshotStore(initialUploadSnapshot()), {
          limits: () => readMaterialLimits(BANANA_MCP_URL, BANANA_MCP_TOKEN),
          project: async handle => {
            const preferences = pageCount(sessionId)
            if (preferences.state.getSnapshot().status !== 'ready') {
              try { await preferences.load() }
              catch { if (!handle) throw new MaterialUploadError('PROJECT_CONTEXT_DENIED') }
            }
            const saved = preferences.state.getSnapshot()
            const current = saved.status === 'ready' ? saved.currentRequirements : null
            const projectHandle = handle ?? current?.binding?.project_handle ?? null
            if (!projectHandle && !current) throw new MaterialUploadError('PROJECT_CONTEXT_DENIED')
            const client = await connectBananaClient(BANANA_MCP_URL, BANANA_MCP_TOKEN)
            try {
              const result = await client.callTool(projectHandle ? 'get_presentation_workflow' : 'create_presentation', projectHandle ? {project_handle:projectHandle} : {idea_prompt:translate('material.projectIdea')})
              const value = result.structuredContent
              if (result.isError || !value || typeof value !== 'object' || Array.isArray(value)) throw new MaterialUploadError('PROJECT_CONTEXT_DENIED')
              const data = value as Record<string,unknown>
              const resolved = typeof data.project_handle === 'string' ? data.project_handle : projectHandle
              if (!resolved) throw new MaterialUploadError('PROJECT_CONTEXT_DENIED')
              const id = typeof data.project_id === 'string' ? data.project_id : undefined
              const brief=data.brief&&typeof data.brief==='object'?data.brief as Record<string,unknown>:{}
              await preferences.bindProject(resolved,brief.task_mode==='relayout'||data.relayout&&typeof data.relayout==='object'?'relayout':'create',!!projectHandle)
              actions.setProject(resolved,id)
              return {handle:resolved, ...(id ? {id} : {}), materialCount:materialIds(data).length}
            } finally { client.dispose() }
          },
          upload:(file,input,signal,progress)=>uploadBinaryMaterial(BANANA_MCP_URL,BANANA_MCP_TOKEN,file,input,signal,progress),
          bind:binding=>bindMaterialContext(sessionId,binding),
          ready:project=>{actions.setProject(project.handle,project.id)},
          persist:draft=>{if(draft.batches.length)sessionStorage.setItem(storageKey,JSON.stringify(draft));else sessionStorage.removeItem(storageKey)},
        })
        // op179: staged (unconfirmed) files also count, so Enter/send works with only files attached.
        const updateAttachments = () => input.setExternalAttachments('wuyu-materials', queue.submissionCount() + queue.stagedCount())
        const removeObserver = queue.state.subscribe(updateAttachments)
        // op179: sending while files are unconfirmed starts the same upload as 确认本批材料 and sends the
        // message automatically once the batch is ready (the upload runs outside the submission).
        const autoSendNotice = '正在上传本批资料，上传完成后会自动发送这条消息。'
        let autoSendPending = false
        const removeModifier = ctx.conversation.registerSubmissionModifier(sessionId, text => {
          if (queue.blocksSubmission()) {
            if (queue.startPendingConfirm()) { autoSendPending = true; input.notify('info', autoSendNotice); throw new Error(autoSendNotice) }
            input.notify('error', translate('upload.confirmFirst'))
            throw new Error(translate('upload.confirmFirst'))
          }
          return queue.prepareSubmission(text)
        })
        const removeAutoSend = queue.state.subscribe(() => {
          if (!autoSendPending) return
          if (queue.blocksSubmission()) { if (queue.selectionStalled()) { autoSendPending = false; input.notify('error', translate('upload.confirmFirst')) } return }
          autoSendPending = false
          setTimeout(() => { const current = input.state.getSnapshot(); if (current.phase === 'plain') input.submit('queue') }, 80)
        })
        const owned = { controller: queue, dispose: () => { removeModifier(); removeObserver(); removeAutoSend(); input.setExternalAttachments('wuyu-materials',0); queue.dispose() } }
        entry = owned
        uploads.set(sessionId, owned)
        actx.effect(() => () => { owned.dispose(); if (uploads.get(sessionId) === owned) uploads.delete(sessionId) }, 'wuyu-material-uploads.scope')
        try { const saved=sessionStorage.getItem(storageKey);if(saved)void queue.restore(JSON.parse(saved)) } catch { /* Disabled browser storage leaves uploads usable for this page. */ }
      }
      const controller = entry.controller
      return {
        hooks:{materialUploads:controller.state},
        intakeFiles:async(files:File[],role:import('./material-upload.ts').MaterialRole,handle:string|null)=>{
          await controller.add(files,role,handle)
        },
        retryUpload:(id?:string)=>controller.retry(id),
        pauseUpload:(id:string)=>controller.pause(id),
        removeUpload:(id:string)=>controller.remove(id),
        replaceUpload:(id:string,file:File)=>controller.replace(id,file),
        syncUploads:()=>controller.sync(),
        confirmUploads:async()=>{await controller.confirm()},
        openDetails:()=>ctx.layout.openDetails(),
      }
  }
  ctx.slots.inject('conversation.input.right', () => ctx.slots.register({
    name:'conversation.input.right',id:'wuyu-work-voice',priority:-10,locale:NS,
    inject:(sessionId:SessionId)=>{
      const actx=ctx.sessions.scope(sessionId);if(!actx)throw new Error('Voice requires a Session scope')
      const input=ctx.conversation.input.for(actx)
      let voice=voices.get(sessionId)
      if(!voice){
        const state={active:false,dispose:()=>{}}
        const remove=ctx.conversation.registerSubmissionModifier(sessionId,text=>{if(state.active)throw new Error(translate('voice.finishFirst'));return text})
        state.dispose=()=>{state.active=false;remove()};voice=state;voices.set(sessionId,state)
        actx.effect(()=>()=>{state.dispose();if(voices.get(sessionId)===state)voices.delete(sessionId)},'wuyu-work-voice.scope')
      }
      const state=voice
      return {
        loadVoiceConfig:async()=>{
          const response=await fetch('/api/voice/work/config',{credentials:'same-origin',cache:'no-store',redirect:'error'})
          const data:unknown=await response.json()
          if(!response.ok||!data||typeof data!=='object')throw new Error('VOICE_CONFIG_UNAVAILABLE')
          const config=data as Record<string,unknown>
          if(typeof config.ready!=='boolean'||!Number.isSafeInteger(config.max_seconds)||Number(config.max_seconds)<1||Number(config.max_seconds)>120||!Number.isFinite(config.recognition_timeout)||Number(config.recognition_timeout)<1||Number(config.recognition_timeout)>180)throw new Error('VOICE_CONFIG_INVALID')
          return {ready:config.ready,max_seconds:Number(config.max_seconds),recognition_timeout:Number(config.recognition_timeout)}
        },
        appendVoiceText:(text:string)=>{const snapshot=input.state.getSnapshot();if(snapshot.phase!=='plain')throw new Error('VOICE_INPUT_BUSY');input.setDraft(snapshot.draft?snapshot.draft+'\n'+text:text)},
        voiceActive:(active:boolean)=>{state.active=active},
      }
    },
  },WorkVoiceInput))
  ctx.slots.inject('conversation.input.left', () => ctx.slots.register({
    name: 'conversation.input.left', id: 'wuyu-presentation-preferences', priority: -20, locale: NS, store,
    inject: (sessionId: SessionId) => {
      searchMode(sessionId)
      return preferenceInjected(sessionId)
    },
  }, PresentationPreferences))
  ctx.slots.inject('conversation.input.left', () => ctx.slots.register({
    name:'conversation.input.left', id:'wuyu-search-mode', locale:NS,
    inject:(sessionId:SessionId)=>{searchMode(sessionId);return {hooks:{preferences:pageCount(sessionId).state},toggleSearchMode:(mode:ArmedSearch)=>{void pageCount(sessionId).toggleSearch(mode).catch(()=>{const actx=ctx.sessions.scope(sessionId);if(actx)ctx.conversation.input.for(actx).notify('error',translate('preferences.failed'))})}}},
  },SearchModeToggle))
  ctx.slots.inject('conversation.input.right', () => ctx.slots.register({
    name:'conversation.input.right', id:'wuyu-ppt-materials', locale:NS, store, inject:uploadInjected,
  },ComposerMaterials))
  ctx.slots.inject('conversation.input.materials', () => ctx.slots.register({
    name:'conversation.input.materials', id:'wuyu-task-entry', priority:-30, locale:NS, store,
    inject:(sessionId:SessionId,actions:BoundActions<typeof store>)=>{const preferences=preferenceInjected(sessionId),upload=uploadInjected(sessionId,actions);return {...preferences,...upload,hooks:{...preferences.hooks,...upload.hooks}}},
  },PresentationTaskMode))
  ctx.slots.inject('conversation.input.materials', () => ctx.slots.register({
    name:'conversation.input.materials', id:'wuyu-material-cards', locale:NS, store, inject:uploadInjected,
  },ComposerMaterialCards))
  ctx.slots.inject('wuyu.work.templates', () => ctx.slots.register({
    name:'wuyu.work.templates', locale:NS, store, inject:templateInjected,
  },TemplateDecisionSurface))
}
