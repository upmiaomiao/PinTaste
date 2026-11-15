/** Fixed public labels; never copy provider output or raw job fields into progress. */
export function workStageLabel(stage, traced = 0, total = 0) {
    if (stage === "planning")
        return "正在读取完整材料并生成大纲";
    if (stage === "research")
        return "正在检索资料并整理调研总结";
    if (stage === "tracing")
        return `正在描摹 ${traced}/${total} 页`;
    if (stage === "quality_check")
        return "正在检查 PPT 文件";
    if (stage === "exporting")
        return "正在导出 PPT 文件";
    return undefined;
}
