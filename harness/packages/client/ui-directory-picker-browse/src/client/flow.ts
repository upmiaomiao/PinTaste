/**
 * The browse picking occupant (package-internal; the `./client` surface
 * exposes only the Loader exports). Same-package tests exercise it directly
 * through this module.
 */
import { createElement } from 'react'
import type { ReactElement } from 'react'
import type { DirectoryListing } from '@deepseek-ai/dsh-api-remotes/client'
import type { Translate } from '@deepseek-ai/dsh-client-locale/client'
// Type-only: the owner contract of the directory-flow holes.
import type { DirectoryFlowOwnerProps } from '@deepseek-ai/dsh-client-ui-workspace/client'
import { DirectoryBrowser } from './DirectoryBrowser.tsx'
import { NamedWorkspaceForm } from './NamedWorkspaceForm.tsx'
import type { NamedWorkspace } from './managed-workspace.ts'

/** Injected face: the browse wire calls and copy the dialog drives (bound in apply's closure). */
export interface BrowseFlowInjected {
  /** Explicit managed Work entry; other browser compositions retain directory picking. */
  managedMode?: boolean
  /** Register only a display name through the authenticated tenant provider. */
  createNamedWorkspace?: (name: string, requestId: string, signal: AbortSignal) => Promise<NamedWorkspace>
  /** List one directory level (absent path = the Host home directory); the signal aborts a superseded scan. */
  listDirectory: (path?: string, signal?: AbortSignal) => Promise<DirectoryListing>
  /** Create one child directory under an existing parent. */
  createDirectory: (path: string, name: string) => Promise<string>
  /** Localized dialog copy (this package's namespace). */
  t: Translate
}

/**
 * Flow occupant: adapts the hole's owner conversation onto the browser
 * dialog — a confirmed directory is the picked path, dismissal is the
 * cancellation. Browse failures (unreadable targets, create conflicts) stay
 * inside the dialog's own alert surfaces, so the owner's `onError` arm is
 * never driven by this occupant.
 * @param props - owner conversation plus the injected browse face.
 * @returns the dialog element (renders nothing while closed).
 */
export function BrowseDirectoryFlow(props: DirectoryFlowOwnerProps & BrowseFlowInjected): ReactElement {
  if (props.managedMode === true) {
    if (props.createNamedWorkspace === undefined) throw new Error('Managed workspace provider is missing')
    return createElement(NamedWorkspaceForm, {
      open: props.open, busy: props.busy, t: props.t,
      createNamedWorkspace: props.createNamedWorkspace, onCreated: props.onCreated, onClose: props.onCancel,
    })
  }
  return createElement(DirectoryBrowser, {
    open: props.open,
    busy: props.busy,
    listDirectory: props.listDirectory,
    createDirectory: props.createDirectory,
    t: props.t,
    onOpen: props.onPicked,
    onClose: props.onCancel,
  })
}
