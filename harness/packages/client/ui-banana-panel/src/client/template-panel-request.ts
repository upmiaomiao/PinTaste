import type { TemplateDecisionInjected } from './TemplateDecisionPanel.tsx'
import {isTemplateActionFailure} from './template-action-error.ts'

/** Serve only this mounted project's design and material-management UI; it cannot dispatch generation or model actions.
 * @param handle - Capability bound by the Host, never supplied by the iframe payload.
 * @param raw - A request from the attached, first-party template tab.
 * @param api - Existing session-scoped, ownership-checked template operations.
 * @returns The safe template controller snapshot after the requested UI operation.
 */
export async function templatePanelRequest(handle: string, raw: unknown, api: TemplateDecisionInjected): Promise<unknown> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('TEMPLATE_PANEL_REQUEST_INVALID')
  const request = raw as Record<string, unknown>
  if ('project_handle' in request || 'project_id' in request) throw new Error('TEMPLATE_PANEL_REQUEST_INVALID')
  const id = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(value)
  switch (request.operation) {
    case 'recommend': await api.loadRecommendations(handle); break
    case 'list': {
      const search = request.search
      const offset = request.offset ?? 0
      if (typeof search !== 'string' || search.length > 200 || !Number.isSafeInteger(offset) || Number(offset) < 0) throw new Error('TEMPLATE_PANEL_REQUEST_INVALID')
      const scope = request.scope ?? 'all'
      if (scope !== 'all' && scope !== 'mine' && scope !== 'public') throw new Error('TEMPLATE_PANEL_REQUEST_INVALID')
      const category = request.template_category
      if (category !== undefined && (typeof category !== 'string' || !['整套PPT', '工艺流程图', '技术路线', '摘要图', '论文方法图'].includes(category))) throw new Error('TEMPLATE_PANEL_REQUEST_INVALID')
      const limit = request.limit ?? 50
      if (!Number.isSafeInteger(limit) || Number(limit) < 1 || Number(limit) > 50) throw new Error('TEMPLATE_PANEL_REQUEST_INVALID')
      await api.loadTemplateLibrary({ search, scope, ...(category === undefined ? {} : { template_category: category }), offset: Number(offset), limit: Number(limit) }, 'library'); break
    }
    case 'detail':
      if (!id(request.source_id)) throw new Error('TEMPLATE_PANEL_REQUEST_INVALID')
      await api.inspectTemplate(request.source_id); break
    case 'materials': {
      await api.loadTemplateMaterials(handle)
      const snapshot=api.hooks.templateLibrary.getSnapshot()
      // Read-only limit recovery is independent of an earlier confirmation failure.
      return snapshot.templateLimitsStatus==='ready'?{...snapshot,actionError:false}:snapshot
    }
    case 'confirm': {
      const source = id(request.source_id), material = id(request.material_id)
      if (source === material) throw new Error('TEMPLATE_PANEL_REQUEST_INVALID')
      const choice = source ? { source_id: request.source_id as string } : { material_id: request.material_id as string }
      if (!await api.confirmTemplateChoice(handle, choice)){
        const snapshot=api.hooks.templateLibrary.getSnapshot()
        // The UI-only envelope transports safe failure, never confirmation success.
        if(isTemplateActionFailure(snapshot.actionFailure,'confirm'))return {...snapshot,actionError:true}
        throw new Error('TEMPLATE_CONFIRMATION_NOT_ACCEPTED')
      }
      break
    }
    case 'delete_material': {
      if (Object.keys(request).some(key => !['operation', 'material_id', 'confirmed', 'expected_revision', 'request_id'].includes(key)) ||
        !id(request.material_id) || request.confirmed !== true || !Number.isSafeInteger(request.expected_revision) || Number(request.expected_revision) < 0 ||
        typeof request.request_id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(request.request_id)) throw new Error('MATERIAL_PANEL_REQUEST_INVALID')
      return api.deleteMaterial(handle, request.material_id, Number(request.expected_revision), request.request_id)
    }
    case 'upload':
      if (!(request.file instanceof File)) throw new Error('TEMPLATE_PANEL_REQUEST_INVALID')
      await api.uploadPrivateTemplate(handle, request.file); break
    default: throw new Error('TEMPLATE_PANEL_REQUEST_INVALID')
  }
  return api.hooks.templateLibrary.getSnapshot()
}
