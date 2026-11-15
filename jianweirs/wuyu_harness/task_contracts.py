"""General evidence guidance; not a sensor checklist or a report template."""
from copy import deepcopy

from .schemas import TASK_LABELS, TaskKind


_CONTRACTS = {
    TaskKind.DISCOVERY: {
        "user_intents": ["有没有堆存", "发现疑点", "是不是裸地"],
        "required_data": ["覆盖真实目标的高分近景与同源周边场景", "坐标、覆盖与图源偏置核验；实际日期未知时如实保留"],
        "followup_triggers": ["目标在边缘或像素不足时补近景或周边", "形态混淆或来源冲突时取能解决疑点的另一来源；足够回答即可停止"],
        "response_fields": ["疑点是否可复核及具体位置", "关键图号与可见证据", "主要竞争解释及缺口"],
        "output_boundaries": ["裸地、施工扰动不自动等于固废", "底图供应商名称不等于独立卫星、日期或原生分辨率"],
    },
    TaskKind.CLASSIFY: {
        "user_intents": ["像哪类东西", "建筑料还是土", "混合物料能分吗"],
        "required_data": ["足以分辨目标纹理与形态的高分裁剪及场地上下文", "现场资料保留来源，与独立影像观察分开"],
        "followup_triggers": ["目标太小先补清晰裁剪", "仅当具体疑点可被光谱或SAR减少时才补相应数据，并检查原生波段、尺度与质量"],
        "response_fields": ["直接可见特征", "候选类别的支持与反证", "正常施工、原料或农业物料等解释", "需现场确认的成分"],
        "output_boundaries": ["RGB、NDVI或单次SAR亮暗不能确认化学成分、危险属性或违法", "用户已有分类不是专业模型独立识别结果"],
    },
    TaskKind.EXTENT: {
        "user_intents": ["范围有多大", "圈边界", "占了多少地"],
        "required_data": ["目标边界清晰的高分影像", "面积测量需要可靠地理参考或GSD、坐标与边界方法；原表数值需注明单位"],
        "followup_triggers": ["边界遮挡或目标在图外时补局部和周边", "道路、厂房、硬化地与阴影易混入时逐区复核"],
        "response_fields": ["可复核边界或像素框", "面积能否测量、单位与边界误差", "遮挡和未覆盖范围"],
        "output_boundaries": ["缺地理参考时不报伪精确平方米", "二维面积不能直接推体积、堆高或吨数"],
    },
    TaskKind.CHANGE: {
        "user_intents": ["历史怎么变", "何时开始", "增加还是减少"],
        "required_data": ["用户时间范围内有实际日期的基准、中间与后期影像", "同点位配准、局部质量和尺度可比性；科学量化需原生波段与定标"],
        "followup_triggers": ["先检索真实存档，再按缺口补时相；局部云雾则换景或相邻日期", "区分农事、植被、含水与堆积变化，需要时补多光谱或同轨同极化SAR"],
        "response_fields": ["实际成像日期与逐期观察", "变化方向及支持程度", "首次可见与此前未见之间的区间", "未分析时段与混杂因素"],
        "output_boundaries": ["当前无日期底图不能充当历史基线", "整景云量不代表目标可见；目录完整不代表像元全部分析"],
    },
    TaskKind.CLEANUP: {
        "user_intents": ["清了多少", "整改是否到位", "有没有真清运"],
        "required_data": ["原堆存基线和有日期的清理中、清理后同点位图", "整改时间与验收资料保留来源；比例量化需可比边界与尺度"],
        "followup_triggers": ["检查清运与遮盖、覆土、平整的区别", "主堆减少后检查边角残留或移到邻近地块的可能"],
        "response_fields": ["哪些区出现可见清理变化", "残留或转移线索", "清理比例是否有依据", "缺哪一阶段或验收证据"],
        "output_boundaries": ["变平或变绿不能单独证明清运完成", "只有新增、增加清单时不能假设已开展整改"],
    },
    TaskKind.RESIDUAL: {
        "user_intents": ["有没有残留", "主堆没了边上呢", "小堆清干净了吗"],
        "required_data": ["原堆边界和整改后的目标近景", "树下、围挡、出入口与散落区的可见性及分辨率"],
        "followup_triggers": ["主堆消失仍检查边角与周边小目标", "低分辨率无法看清时补高分，遮挡区保持未知"],
        "response_fields": ["残留候选位置与图块", "裸地、正常物料等反例", "无法看清的区域"],
        "output_boundaries": ["未看到小堆不等于全部清零", "10米级混合像元通常不足以排除小型残留"],
    },
    TaskKind.RECURRENCE: {
        "user_intents": ["清后又堆了吗", "反复出现", "没清完还是又倒了"],
        "required_data": ["原堆存、已清理、再次出现三个阶段的实际日期与同点位证据", "复发备注仅作用户资料先验，保留清理基线来源"],
        "followup_triggers": ["缺已清空的中间基线时定向补该阶段", "排查未清完、暂时遮盖、移到旁边或正常施工周转"],
        "response_fields": ["三阶段分别有哪些证据", "复发假设与竞争解释", "只能验证哪一段、还缺什么"],
        "output_boundaries": ["两幅前后图或一句备注不能独立确认清后复发", "重复扰动不直接等于违法倾倒"],
    },
    TaskKind.TRACKING: {
        "user_intents": ["跟踪这个点", "按月回溯", "从某时到现在"],
        "required_data": ["稳定目标身份、用户起止范围与数据截止日", "跨期真实目录、代表影像及每期覆盖、读取与质量状态"],
        "followup_triggers": ["早中近期不均衡时优先补缺失阶段", "区分目录无命中、权限不足、传输失败和局部不可用，再选替代源"],
        "response_fields": ["日期时间轴与逐期状态", "持续、扩大、清理或复发的证据", "完整检索范围与实际分析抽样范围"],
        "output_boundaries": ["检索全目录不能称为逐景完成历史分析", "本次回溯不自动建立未来定时监控"],
    },
    TaskKind.ENVIRONMENT: {
        "user_intents": ["周边环境", "有什么环境风险", "现场先查什么"],
        "required_data": ["目标及水体、居民区、耕地的同范围影像或GIS", "推断迁移路径需地形排水资料；量化距离需可靠地理参考"],
        "followup_triggers": ["只有堆体特写时补环境场景", "径流方向、受体或地块性质不明时补资料，不靠印象填空"],
        "response_fields": ["可见敏感目标与空间依据", "源、可能路径与受体", "现场优先核实项及未知"],
        "output_boundaries": ["邻近水体、耕地或居民区不等于已污染", "影像不能给污染浓度或法定风险等级"],
    },
    TaskKind.RELATIONSHIP: {
        "user_intents": ["和道路厂界什么关系", "是否占到农田", "鱼塘是否被填"],
        "required_data": ["目标、道路、厂界、水体与地块边界的同点位场景", "法律边界判断需地籍、河道管理或耕地保护边界等资料"],
        "followup_triggers": ["场景不足时扩大范围且保留目标身份", "有偏置或相邻疑点时核对同名地物，避免串图串点"],
        "response_fields": ["道路、水体、农田或厂界的相对关系", "图号、尺度与空间依据", "待核对的边界或归属资料"],
        "output_boundaries": ["道路相连不能确认运输来源或责任单位", "影像可见农田不直接等于永久基本农田或违法占地"],
    },
}


def task_contracts(kinds=None) -> list[dict]:
    """Return isolated, JSON-safe guidance for selected kinds, or all ten."""
    selected = list(TaskKind) if kinds is None else [TaskKind(kind) for kind in kinds]
    return [{"task_type": kind.value, "label": TASK_LABELS[kind],
             "application_rule": "仅在用户要求此监管结论时使用；目录或纯科学追问优先本轮research模式。这些数据条件不要求每次全部取源，也不规定回答格式。",
             **deepcopy(_CONTRACTS[kind])}
            for kind in dict.fromkeys(selected)]


def planner_task_guidance(research_mode="auto", include_optical_context=False) -> dict:
    """Keep targeted research questions ahead of regulatory-task guidance."""
    research_only = research_mode == "availability" or (research_mode != "auto" and not include_optical_context)
    return {
        "version": "task-evidence-contracts-v1",
        "application": "research_only" if research_only else "select_by_current_question",
        "research_mode": research_mode,
        "selection_rule": "按本轮自然语言选择task_types，可组合；不要因为默认分类或父任务执行无关任务。数据要求是结论条件，不是必须调用所有传感器或固定回答模板。",
        "contracts": [] if research_only else task_contracts(),
        "research_priority": "本轮明确要求目录、科学分析或时序时优先research模式。底图配准问题不阻塞独立坐标目录查询。",
        "response_fields": (["按实际来源、实际日期与期间回答", "区分目录命中、像元读取、权限与局部质量", "区分完整检索范围与实际分析时相"]
                            if research_mode != "auto" else []),
    }
