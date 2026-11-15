/** Keep a submission identity across network failures; never retry paid work blindly. */
export class SubmissionKeys {
  constructor(createKey = () => crypto.randomUUID()) {
    this.createKey = createKey;
    this.pending = new Map();
  }
  begin(userId, path, method, body) {
    if (
      method !== "POST" ||
      !(
        path === "/tasks" ||
        /^\/(uploads\/[^/]+\/workbook\/batch|complaints\/scans\/[^/]+\/start)$/.test(
          path,
        )
      )
    )
      return null;
    const signature = JSON.stringify([userId, path, body]);
    if (!this.pending.has(signature)) {
      if (this.pending.size >= 128)
        throw new Error("存在较多尚未确认的提交，请先查看历史任务。");
      this.pending.set(signature, this.createKey());
    }
    return { signature, key: this.pending.get(signature) };
  }
  finish(ticket, response) {
    if (
      ticket &&
      (response.ok ||
        (response.status < 500 &&
          response.headers.get("X-Submission-State") !== "pending"))
    ) {
      this.pending.delete(ticket.signature);
    }
  }
  clear() {
    this.pending.clear();
  }
}
