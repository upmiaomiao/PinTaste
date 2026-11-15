const record = (value) => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
/** History stays in the snapshot; progress and failures belong to current inputs. */
export function currentWorkflowJobs(snapshot) {
    const data = record(snapshot), epoch = data.epoch;
    const jobs = Array.isArray(data.jobs) ? data.jobs.map(record) : [];
    if (typeof epoch !== 'number' || !Number.isInteger(epoch))
        return jobs;
    return jobs.filter(job => typeof job.input_epoch !== 'number' || !Number.isInteger(job.input_epoch) || job.input_epoch === epoch);
}
