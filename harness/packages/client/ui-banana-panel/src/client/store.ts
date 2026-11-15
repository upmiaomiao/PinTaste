/**
 * Per-Session Banana Slides panel state shared by the transcript row and the
 * right details column. The tool.call.toolview row writes the current project
 * handle (via its inject-bound actions) and the details-column panel reads it,
 * so both stay in one Session scope.
 */
import { defineStore, type EngineStoreHandle } from '@deepseek-ai/dsh-client-store'
import type { PreviewTarget } from './preview-target.ts'
import type { ResearchCard } from './research-conversation.ts'
/** Slide image-generation mode selected in the composer (per Session). */
export type ImageMode = 'stable' | 'creative' | 'best'
/** Slide style preset id selected in the composer (per Session). `default` means no override. */
export type StyleMode =
  | 'default'
  | 'business-simple'
  | 'tech-modern'
  | 'academic-formal'
  | 'creative-fun'
  | 'minimalist-clean'
  | 'luxury-premium'
  | 'nature-fresh'
  | 'gradient-vibrant'
/** Slide template image preset id selected in the composer (per Session). `default` means no template image. */
export type TemplateMode =
  | 'default'
  | 'business-plan'
  | 'circular-economy'
  | 'phd-defense-1'
  | 'phd-defense-2'
  | 'xuejian'
  | 'solid-waste'
/** A user-uploaded template selected in the composer (per Session). */
export interface UserTemplateRef {
  /** Backend template id (UUID). */
  id: string
  /** Display name shown in the composer and the model-facing prefix. */
  name: string
}
/** State shared across the conversation row, the composer mode control, and the right details panel. */
export interface BananaPanelState {
  /** Opaque project handle of the deck shown in the details column, or null. */
  projectHandle: string | null
  projectId: string | null
  projects: Record<string, string>
  researchAnchor: ResearchCard | null
  previewTarget: PreviewTarget
  previewRequest: number
  panelView:'project'|'templates'
  /** Slide image-generation mode chosen in the composer; defaults to best. */
  imageMode: ImageMode
  /** Slide style preset chosen in the composer; defaults to no override. */
  styleMode: StyleMode
  /** Slide template image preset chosen in the composer; defaults to none. */
  templateMode: TemplateMode
  /** User-uploaded template chosen in the composer; null means none. */
  userTemplate: UserTemplateRef | null
}
type BananaPanelActions = {
  setProject: (draft: BananaPanelState, handle: string, projectId?: string) => void
  observeResearch: (draft: BananaPanelState, card: ResearchCard) => void
  openPreview: (draft: BananaPanelState, target: PreviewTarget) => void
  showTemplates:(draft:BananaPanelState)=>void
  setImageMode: (draft: BananaPanelState, mode: ImageMode) => void
  setStyleMode: (draft: BananaPanelState, mode: StyleMode) => void
  setTemplateMode: (draft: BananaPanelState, mode: TemplateMode) => void
  setUserTemplate: (draft: BananaPanelState, selection: UserTemplateRef | null) => void
}
/**
 * Create the Banana panel store handle.
 * @returns a handle instantiated once per rendered Session scope.
 */
export function createBananaPanelStore(): EngineStoreHandle<BananaPanelState, BananaPanelActions> {
  return defineStore({
    init: (): BananaPanelState => ({ projectHandle: null, projectId: null, projects: {}, researchAnchor: null, previewTarget: {view:'outline'}, previewRequest:0,panelView:'project', imageMode: 'best', styleMode: 'default', templateMode: 'default', userTemplate: null }),
    actions: {
      setProject: (draft, handle, projectId) => { draft.projectHandle = handle; draft.projectId = projectId || null; if(projectId)draft.projects[projectId] = handle },
      observeResearch: (draft, card) => { if (!draft.researchAnchor || draft.researchAnchor.seq < card.seq) draft.researchAnchor = card },
      openPreview: (draft, target) => { if(target.project_id){const h=draft.projects[target.project_id];if(!h)return;draft.projectHandle=h;draft.projectId=target.project_id}draft.previewTarget=target;draft.previewRequest++;draft.panelView='project' },
      showTemplates:draft=>{draft.panelView='project';draft.previewTarget={view:'templates'};draft.previewRequest++},
      setImageMode: (draft, mode) => { draft.imageMode = mode },
      setStyleMode: (draft, mode) => { draft.styleMode = mode },
      setTemplateMode: (draft, mode) => { draft.templateMode = mode },
      setUserTemplate: (draft, selection) => { draft.userTemplate = selection },
    },
  })
}
