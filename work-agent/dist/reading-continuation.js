export function canContinueWithOmissions(reading) {
    const option = reading.continuation_options || {}, issues = Array.isArray(reading.issues) ? reading.issues : [];
    return reading.coverage_complete !== true && reading.accepted_with_omissions !== true && option.can_continue_with_omissions === true &&
        option.content_plan_valid === true && option.failure_category === 'source_unread' && Number(option.verified_findings) > 0 &&
        !issues.some((issue) => /quota|policy|security|hash|scope|unsafe|forbidden|identity|credential|provider_config|model_policy|invalid_slide_spec|schema|synthesis_invalid/i.test(String(issue?.code || '')));
}
