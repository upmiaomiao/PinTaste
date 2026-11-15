import { z } from "zod/v4";
// Visible page numbers are one-based; never guess from an array index or UUID.
export const pageRevisionPayload = z.object({
    number: z.number().int().min(1).max(80),
    feedback: z.string().trim().min(1).max(12000).optional(),
    change: z.string().trim().min(1).max(12000).optional(),
    copy: z.string().trim().min(1).max(30000).optional(),
}).strict().refine(value => Boolean(value.feedback || value.change) && !(value.feedback && value.change));
/** A complete, current image-version map is supplied only after actual review. */
export const imageReviewPayload = z.object({
    image_versions: z.record(z.string().regex(/^(?:[1-9]|[1-7][0-9]|80)$/), z.number().int().positive())
        .refine(versions => Object.keys(versions).length >= 1 && Object.keys(versions).length <= 80)
        .describe("Complete current page-to-image-version map from the latest conversation.review_target.image_versions; canonical page keys 1–80 and positive integer versions. Never infer missing versions or reuse approval after a version change."),
}).strict();
export const imageReviewInstruction = "confirm_images uses exactly {image_versions:<complete current map>}, taken from the latest get_presentation_workflow conversation.review_target.image_versions and its revision, only after the user's actual same-version decision. Never send an empty payload, invent missing versions or transfer approval. Reading or displaying a map is not approval. Image PPTX delivery uses export with payload={format:'pptx'}; do not omit format or use editable_pptx for an image-only request. Editable conversion remains a separate explicit request within approved cost scope. ";
export const pageRevisionInstruction = "revise_page uses exactly {number:<visible one-based page number>,feedback:<actual user correction>,copy?:<replacement visible copy>}. " +
    "Use number, never page/page_number/page_id or a zero-based index. Read current page wording and revision before editing. " +
    "update_outline already invalidates changed images; after saving page_patches use the returned revision and resume once, rather than submitting a second revise_page for the same text edit. " +
    "For a visual-only change, call revise_page once with number and feedback; it queues that page automatically. " +
    "After any conflict or unknown outcome, re-read state and inspect pending jobs/image versions before deciding whether another action is needed. " +
    "A user's template confirmation click changes the project revision. After that click, always call get_presentation_workflow before continue_from_draft or any other action; use its newly returned revision, never the pre-confirmation revision. " +
    "Pure acceptance is not a requirements edit. Read get_presentation_requirements, use the Host acknowledge_presentation_confirmation only to validate and record the actual confirmation source without a content patch, then read the latest workflow revision before continuing the same valid draft. The source receipt is not business approval or cost permission. If content really changed, follow re-planning and current-version review; never use update_outline to bypass source evidence. " +
    "Do not automatically repeat paid regeneration or transfer image approval across revisions.";
export const editableExportInstruction = "图片版交付和可编辑转换是两个步骤。用户明确要求可编辑版时，先读取最新get_presentation_workflow。若conversation.editable_conversion存在，使用其中action=export和payload={format:'editable_pptx'}，以及最新revision和本次稳定request_id；不要调用start_tracing，不要直接改brief.output_preference，也不要用rebuild:true开始首次转换。该读取指引不是执行许可，仍须遵守用户已有明确请求、页图版本审核及费用确认。status/download等查询不启动转换。start_tracing只用于工作流实际phase=tracing的既有可编辑流程。图片版已经交付且当前版本已审核时，不要重复confirm_images或重新生成页图；当前版本未审核时，先展示并取得该版本真实审核。未知结果或失败先回读任务，不自动重复付费。";
// A closed dictionary is the only backend-derived information exposed here.
// Never echo backend messages, payloads, diagnostics, keys or URLs, including on 400/422.
const errorGuidance = {
    TEMPLATE_READING_IN_PROGRESS: {
        next_action: "wait_for_current_reading", refresh_required: true,
        message: "资料阅读正在进行，当前模板修改未接受。请等本轮结束后再确认或更换模板；现有阅读继续，不会因此重新派发。",
        instruction: "Read the existing reader job and use bounded waiting. The template mutation was rejected and did not cancel or restart reading. Do not retry it, cancel the reader, change style to force it through, or dispatch another paid read. After the run ends, re-read state before the user confirms or changes the template.",
    },
    TEMPLATE_PARSE_PENDING: {
        next_action: "wait_for_template_parsing", refresh_required: true,
        message: "模板仍在本地解析，本次资料阅读尚未启动。请等待同一解析任务完成；不要重复上传或重复启动阅读。",
        instruction: "Wait for the already accepted local template parse. No new paid reading was started by the rejected action. Preserve the upload and project; do not re-upload or enqueue another reading job. Re-read state after parsing, and complete the actual user template confirmation before reading when that choice is pending.",
    },
    TEMPLATE_PARSE_INCOMPLETE: {
        next_action: "inspect_template_parse", refresh_required: true,
        message: "模板解析尚未完成，当前没有覆盖它的排队或运行中解析任务。请刷新并检查当前模板解析；原文件保留，本次没有启动阅读。",
        instruction: "Read the current template and parser jobs. There is no active parser covering this pending template, so do not keep reporting that it is processing. Preserve the original, inspect local parse recovery in the same project, and do not re-upload, enqueue paid reading or retry automatically. A known failed or cancelled parser may use the existing explicit local retry only after its current state and the user request are checked.",
    },

    DRAFT_REVISION_CONFLICT: {
        next_action: "inspect_current_draft", dispatch_outcome: "rejected_before_dispatch",
        message: "草稿对应的材料或版本已变化，本次继续未被接受，也未派发新的生成任务。请先读取当前需求、草稿版本和任务状态；现有材料与结果保留。",
        instruction: "This continue request was rejected before dispatch. Read get_presentation_requirements and get_presentation_workflow; inspect the current draft revision, reading status and pending jobs. Pure acceptance is not a content change: never copy approval wording into requirements, topic or design_notes. For an unchanged actual user confirmation, use the Host tool acknowledge_presentation_confirmation to validate and record only its source, with no content patch; this receipt is not business approval or cost permission. Then read the latest workflow revision before continue_from_draft, and continue only if the current draft remains ready and the actual user accepted that same draft. If content or source scope truly changed, use the normal re-planning and review path within approved cost scope. Do not restore an old approval, bypass evidence with update_outline, re-upload, create a project, or automatically repeat paid work.",
    },
    DRAFT_NOT_READY: {
        next_action: "inspect_current_draft", dispatch_outcome: "rejected_before_dispatch",
        message: "当前草稿尚不满足继续条件，本次继续未被接受，也未派发新的生成任务。请先核对阅读状态和当前草稿；不要把这一拒绝当成网络超时或重复生成。",
        instruction: "This continue request was rejected before dispatch because the current draft is not ready. Read the current workflow, reading status, draft revision, continuation options and existing jobs. Do not fabricate a ready draft or use update_outline to bypass source evidence and review. A pure acceptance must not update requirements; use the Host tool acknowledge_presentation_confirmation only to validate and record the actual confirmation source with no content patch, then read the latest workflow revision. That source receipt does not approve business work or additional cost. Continue only after the current valid draft has the user's actual same-version acceptance. If real content changed, follow normal re-planning and review; do not retry reading or generation automatically.",
    },
    DRAFT_REVIEW_REQUIRED: {
        next_action: "review_current_draft", requires_user_confirmation: true,
        message: "当前材料草稿尚未确认。请先阅读这版逐页内容并在原对话明确是否继续；无需重新上传材料或新建项目。",
        instruction: "Read the current material draft and draft_revision, show the actual content, and obtain explicit user acceptance before continue_from_draft. Do not treat this as a version conflict, re-read all materials, or invent approval.",
    },
    RESEARCH_REVIEW_REQUIRED: {
        next_action: "review_research_in_ui", requires_user_confirmation: true,
        message: "当前调研资料仍需检查。请先查看来源、总结和缺失项，再由用户通过现有资料调研界面选择如何继续。",
        instruction: "Read the current research report, sources and omissions. The user must make the required research choice through the existing interface; never confirm research for them or automatically repeat a paid search.",
    },
    RESEARCH_CONFIRMATION_REQUIRED: {
        next_action: "obtain_research_consent_in_ui", requires_user_confirmation: true,
        message: "尚未取得本次检索所需的用户确认。需要补充公开资料时，请由用户通过现有联网或资料调研入口确认范围与预算；也可继续使用已有资料。",
        instruction: "Obtain the actual user's research scope and budget confirmation through the existing UI. Saved search preferences are not execution permission. Do not call a research-confirmation action for the user or retry paid search automatically.",
    },
    TEMPLATE_SELECTION_REQUIRED: {
        next_action: "preview_and_confirm_template_in_ui", requires_user_confirmation: true,
        message: "当前模板尚未确认。请先加载实际模板预览，再由用户点击确认使用；现有材料和草稿保留。",
        instruction: "Open the actual template previews and let the user confirm the current template in the interface. Re-read state after that click and use the new revision. Never simulate confirmation or infer it from uploading or viewing.",
    },
    TEMPLATE_CONFIRMATION_UI_REQUIRED: {
        next_action: "preview_and_confirm_template_in_ui", requires_user_confirmation: true,
        message: "模板确认需要用户在实际预览界面点击，模型不能代为确认。请打开预览并保留当前项目。",
        instruction: "Template confirmation is UI-only. Open actual previews for the user and re-read state after their confirmation; do not invoke a confirmation mutation through a model tool.",
    },
    REVISION_CONFLICT: {
        next_action: "read_current_revision",
        message: "项目版本已变化。请先读取当前版本、页图与进行中的任务，再核对本次修改；不要自动重复生成或沿用旧版审核。",
        instruction: "Read the latest revision, page image versions and pending jobs. Compare the actual user intent with current state before any next action; do not automatically repeat paid work or transfer approval to changed images.",
    },
    IDEMPOTENCY_CONFLICT: {
        next_action: "inspect_prior_request",
        message: "请求标识与已有操作不一致。请先核对原操作和任务状态，不要直接更换标识重新提交。",
        instruction: "Inspect the prior request and current jobs before deciding whether anything remains to do. Never reuse an idempotency ID for a different payload or mint a fresh ID merely to repeat an uncertain paid operation.",
    },
    IDEMPOTENCY_CONTEXT_CHANGED: {
        next_action: "inspect_prior_request",
        message: "原请求对应的项目状态已变化。请核对原操作是否已接受及当前版本，不要直接重播旧操作。",
        instruction: "Read the current state and inspect whether the original request was accepted. Its prior context has changed; do not replay a paid action or reuse old image approval automatically.",
    },
    QUOTA_EXCEEDED: {
        next_action: "wait_for_quota", refresh_required: false,
        message: "当前额度不足，已停止继续处理。已有资料和成果保留；额度恢复后由用户决定是否继续。",
        instruction: "Stop work. Read current state without starting any new work; resume only after quota becomes available and the user chooses to continue.",
    },
    IMAGE_REVIEW_INPUT_INVALID: {
        next_action: "read_current_image_versions", dispatch_outcome: "rejected_before_dispatch",
        message: "页图确认参数缺失或格式不正确，本次确认尚未提交。请读取当前页图版本和审核目标，按用户对同版图片的实际决定操作；已有图片保留。",
        instruction: "This malformed confirmation was rejected locally before any API request. Read get_presentation_workflow and obtain the complete current conversation.review_target.image_versions map and revision. Do not fill missing versions, infer user approval, transfer an old approval, regenerate images or start paid conversion. If the current map is missing from the response, report the missing review context instead of guessing. After the user's actual current-version decision, confirm_images accepts only {image_versions:<that complete map>}. Image-only delivery then uses export with {format:'pptx'}; editable conversion needs its own explicit request and approved cost scope.",
    },
    PAGE_REVISION_INPUT_INVALID: {
        next_action: "correct_page_revision_input",
        message: "单页修改参数不正确。请核对当前可见页码，并使用该页实际修改意见；没有接受这次页面修改。",
        instruction: "Read current page numbers and wording. Correct the payload to {number,feedback,copy?} using the actual current page count; no page mutation was accepted.",
    },
    INVALID_WORKFLOW_ACTION: {
        next_action: "inspect_current_step",
        message: "当前步骤不支持这项操作。请读取实际进度及下一步要求，保留当前项目，不要直接重复提交。",
        instruction: "Read the actual workflow step and its required decision. Choose only an action supported by that state and the user's real request; do not automatically translate the rejected action into paid work.",
    },
    WORKFLOW_BUSY: {
        next_action: "inspect_pending_jobs",
        message: "当前项目已有任务在处理。请查看该任务状态，不要重复派发相同工作。",
        instruction: "Read current pending jobs and use bounded waiting for the already accepted task. Do not dispatch an equivalent paid job with a new request ID.",
    },
    reading_cached_plan_invalid: {
        next_action: "request_specific_draft_feedback", requires_user_confirmation: true,
        message: "原文件和已核对的阅读结果保留，旧逐页计划未通过格式或来源引用检查。可在原对话明确要求按原需求重新整理草稿、修正格式和来源引用，无需改变原内容；原样重试不会改变旧计划。",
        instruction: "Read the current draft_revision and retained reading results. Ask for an explicit correction request; the user may say to keep the original requirements and correct response formatting or source references. Do not blame the user or require invented content changes. Use revise_draft with that actual request only within approved cost scope. Do not repeat analyze_materials with identical inputs, invent feedback, silently omit sources, or create a new project. This recovery does not apply to security, source hash, path or cache-integrity errors.",
    },
    FORBIDDEN: {
        next_action: "reopen_authorized_project", refresh_required: false,
        message: "当前项目访问权限无效，请通过已登录账号重新打开有权访问的项目。",
        instruction: "Stop mutations. Reopen an authorized project through the authenticated interface; do not retry with a different identity or expose private diagnostics.",
    },
    UNAUTHORIZED: {
        next_action: "reopen_authorized_project", refresh_required: false,
        message: "登录或项目授权已失效，请重新登录后打开当前账号有权访问的项目。",
        instruction: "Stop mutations. Restore the user's authenticated access through the existing interface; do not retry with a different identity or reveal credentials.",
    },
};
/** Only an exact, whitelisted machine code selects fixed public guidance. */
export function workflowErrorGuidance(payload, status) {
    const outer = payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {};
    const error = outer.error && typeof outer.error === "object" && !Array.isArray(outer.error) ? outer.error : {};
    const code = typeof error.code === "string" && Object.hasOwn(errorGuidance, error.code) ? error.code : null;
    if (code && errorGuidance[code].dispatch_outcome === "rejected_before_dispatch" && status !== (code === "IMAGE_REVIEW_INPUT_INVALID" ? 400 : 409))
        return null;
    return code ? { error_code: code, ...errorGuidance[code] } : null;
}
const recoveryActions = new Set(["request_research", "update_brief", "select_references", "analyze_materials", "revise_draft", "continue_from_draft", "exclude_materials", "exclude_source_assets", "generate_outline", "update_outline", "generate_samples", "regenerate_samples", "generate_focused", "generate_images", "generate_next", "confirm_page", "revise_page", "confirm_images", "start_tracing", "export", "retry", "pause", "resume", "cancel"]);
/** Deliberately omit backend payloads, URLs, private diagnostics and input prose. */
export function actionRecovery(action, status, payload) {
    const known = workflowErrorGuidance(payload, status);
    const code = known?.error_code ?? "WORKFLOW_ACTION_FAILED";
    const accessFailure = status === 401 || status === 403;
    return {
        action: recoveryActions.has(action) ? action : "unknown", error_code: code,
        refresh_required: known?.refresh_required ?? !accessFailure,
        next_tool: "get_presentation_workflow",
        next_action: known?.next_action ?? (accessFailure ? "reopen_authorized_project" : "inspect_current_step"),
        requires_user_confirmation: known?.requires_user_confirmation === true,
        ...(known?.dispatch_outcome === "rejected_before_dispatch" ? { dispatch_outcome: "rejected_before_dispatch" } : {}),
        repeat_paid_action_automatically: false,
        instruction: known?.instruction ?? (action === "start_tracing" && status === 400
            ? "Tracing did not start. Read get_presentation_workflow. If conversation.editable_conversion is present, the image-first deck is already reviewed: follow its export action with format=editable_pptx only for the user's explicit conversion request and approved cost scope. Do not repeat confirm_images, start_tracing, or regenerate images. Otherwise inspect the actual phase and current review_target; obtain any missing current-version approval. No conversion was accepted by this rejected request."
            : accessFailure
                ? "Stop mutations and reopen an authorized project through the authenticated interface. Do not retry with a different identity or reveal credentials."
                : "Read current state to identify the unmet precondition and determine whether the prior action was accepted. An unknown 400/409 is not proof of a version conflict. Preserve the project and existing results; do not repeat paid work automatically or claim a failed action succeeded."),
    };
}

const materialFailureCopy = Object.freeze({
    "invalid_office": [
        "Office文件结构无法读取。请确认原文件可正常打开，另存为有效的DOCX/PPTX/XLSX后补传；其他已完成材料保留。",
        "The Office file structure could not be read. Check that the original opens, then save a valid DOCX/PPTX/XLSX copy and upload it. Other completed materials are retained."
    ],
    "invalid_pdf": [
        "PDF文件无法读取。请确认原文件可正常打开，另存为有效PDF后补传；其他已完成材料保留。",
        "The PDF could not be read. Check that the original opens, then save and upload a valid PDF copy. Other completed materials are retained."
    ],
    "encrypted_material": [
        "材料已加密或需要密码。请在本地解密并另存副本后补传；其他已完成材料保留。",
        "The material is encrypted or needs a password. Decrypt it locally, save a copy and upload that copy. Other completed materials are retained."
    ],
    "material_timeout": [
        "材料本地解析超时，尚未进入模型阅读。请拆分文件后补传，或联系管理员检查本地解析器；已完成材料保留。",
        "Local material parsing timed out before model reading. Split and upload the file, or ask an administrator to check the local parser. Completed materials are retained."
    ],
    "material_limit": [
        "文件超过本地解析大小上限。请压缩或拆分文件后补传；已完成材料保留。",
        "The file exceeds the local parsing size limit. Compress or split it and upload again. Completed materials are retained."
    ],
    "archive_limit": [
        "Office文件解压规模超过本地解析上限。请减少内嵌内容或拆分文件后补传；已完成材料保留。",
        "The unpacked Office file exceeds local parsing limits. Reduce embedded content or split the file and upload again. Completed materials are retained."
    ],
    "unsafe_archive": [
        "Office文件未通过内部结构安全检查，已停止解析。请提供可信的有效副本，或联系管理员核对；原件和已完成材料保留。",
        "The Office file did not pass internal structure safety checks, so parsing stopped. Supply a trusted valid copy or ask an administrator to review it. Originals and completed materials are retained."
    ],
    "unsafe_path": [
        "材料存储路径未通过安全检查，已停止解析。请联系管理员核对文件存储；原件和已完成材料保留。",
        "The material storage path did not pass safety checks, so parsing stopped. Ask an administrator to check file storage. Originals and completed materials are retained."
    ],
    "material_not_found": [
        "材料原文件暂时无法找到。请联系管理员核对存储，确认无法恢复后再补传；已有内容保留。",
        "The original material file could not be found. Ask an administrator to check storage; upload a replacement only if it cannot be restored. Existing content is retained."
    ],
    "invalid_encoding": [
        "文本编码无法读取。请另存为UTF-8文本后补传；其他已完成材料保留。",
        "The text encoding could not be read. Save and upload a UTF-8 text copy. Other completed materials are retained."
    ],
    "invalid_csv": [
        "CSV表格无法读取。请检查表格结构并另存为UTF-8 CSV后补传；其他已完成材料保留。",
        "The CSV table could not be read. Check its structure, save it as UTF-8 CSV and upload again. Other completed materials are retained."
    ],
    "invalid_image": [
        "图片文件无法读取。请确认图片可正常打开，另存为有效图片后补传；其他已完成材料保留。",
        "The image could not be read. Check that it opens, then save and upload a valid image copy. Other completed materials are retained."
    ],
    "image_limit": [
        "图片尺寸超过本地解析上限。请缩小图片尺寸后补传；其他已完成材料保留。",
        "The image dimensions exceed local parsing limits. Resize it and upload again. Other completed materials are retained."
    ],
    "unsupported_material": [
        "当前文件格式不支持本地解析。请转为受支持的PDF、DOCX、PPTX、XLSX或UTF-8文本后补传；已完成材料保留。",
        "This file format is not supported for local parsing. Convert it to supported PDF, DOCX, PPTX, XLSX or UTF-8 text and upload again. Completed materials are retained."
    ],
    "logo_format": [
        "Logo材料须为PNG、JPEG、WebP或TIFF图片。请转为支持的图片格式后补传；其他已完成材料保留。",
        "Logo materials must be PNG, JPEG, WebP or TIFF images. Convert and upload a supported image. Other completed materials are retained."
    ],
    "template_render_failed": [
        "模板页面未能转换为预览，尚未开始生成。请确认原件可正常打开后补传有效副本，或联系管理员检查本地转换器；其他材料保留。",
        "Template pages could not be converted to previews; generation has not started. Upload a valid copy that opens, or ask an administrator to check the local converter. Other materials are retained."
    ],
    "material_project_limit": [
        "材料解析超过项目容量。请缩小本次文件，或在材料管理中移除不需要的文件后再补传；其他已完成材料保留。",
        "Material parsing exceeds project capacity. Reduce this file, or explicitly remove unneeded uploads in material management before uploading again. Other completed materials are retained."
    ],
    "material_parse_failed": [
        "材料本地解析未完成，尚未进入模型阅读。请检查失败文件，补传有效副本或联系管理员核对；其他已完成材料保留。",
        "Local material parsing did not complete before model reading. Check the failed file, upload a valid copy or ask an administrator to review it. Other completed materials are retained."
    ]
});

export function materialFailureView(material, language = "zh") {
    if (!material || String(material.status ?? material.parse_status ?? "").toLowerCase() !== "failed")
        return {};
    let code = typeof material.error_code === "string" ? material.error_code.toLowerCase() : "";
    if (code === "material_project_capacity") code = "material_project_limit";
    if (!Object.hasOwn(materialFailureCopy, code)) code = "material_parse_failed";
    const error_code = code === "material_project_limit" ? "MATERIAL_PROJECT_LIMIT" :
        code === "material_parse_failed" ? "MATERIAL_PARSE_FAILED" : code;
    return { error_code, error_message: materialFailureCopy[code][language === "en" ? 1 : 0] };
}
