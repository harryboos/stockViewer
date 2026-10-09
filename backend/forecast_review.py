"""Versioned forecast review rules and compact, immutable candidate audit data."""
from __future__ import annotations

from copy import deepcopy


REVIEW_INSTRUCTION = (
    "决策复盘规则：先比较全部候选的趋势阶段、催化增量与共同风险，再选0至3个方向；不按输入顺序或当日涨幅直接抄榜。"
    "candidateSignals只表示行情召回通道，不是买入评分或未来胜率；未覆盖方向不能被宣称已研究或不存在机会。"
    "candidateSelection说明实际发现范围：快照数量、历史已读取/对齐数量与入池数量不同，不能将有限读取说成全市场扫描。"
    "discoveryFeatures提供动量变化与相对成交额；momentumChange3d/5d是最近窗口收益减上一相邻窗口收益，单位百分点，"
    "动量改善仍可能是负收益下跌减速，不等于趋势已反转。historyContinuity未验证时不宣称已核对全部交易日。"
    "eventClues只是筛选前的概念名称与新闻匹配，不能当作新利好、首次公开或确定受益；要再次核对主体、时间与传导链。"
    "区分‘初期修复、趋势延续、加速后拥挤、转弱、证据不足’，在每项thesis中说明阶段及未来窗口还剩什么增量。"
    "近5日负收益或仍低于MA20不能单独排除反转候选：若有预测时已公开的新催化，结合change3d、"
    "previousChange5d、momentumChange5d、ma5Slope5d、广度和成交额检验改善；负收益也不能单独当作抄底理由。"
    "momentumChange5d是最近5日收益减此前5日收益，单位百分点；distanceToMa20是当前价格偏离20日均线的百分比；"
    "upDays5d是最近5个收盘价变动中的上涨次数，drawdown5d是这段收盘路径最大回撤。指标没有经验证的固定买卖阈值。"
    "对已大涨方向审查change20d、均线偏离、动量减速和回撤，明确新增催化是否足以抵消已兑现预期；"
    "不得因历史赢家身份、高RSI或高涨幅自动追涨，也不得仅因涨幅大就机械淘汰。"
    "催化判断给出具体主体、受益环节、订单/收入转化路径和反证；首次公开时间未核实时，不得断言这是新催化。"
    "沿产业链寻找有本概念来源支持的间接受益环节，不能把AI等叙事只限定在硬件，也不能把相邻概念的订单当成本概念订单。"
    "公司经营增长、行业合作、股价上涨是不同证据，不能从任何一项直接断言整个板块盈利已兑现；"
    "没有订单或收入证据时只表述需求预期，不编造本地企业与海外事件主体的客户关系。"
    "检查入选方向是否依赖同一产业链、同一客户资本开支或同一新闻：名字不同不等于风险分散；"
    "若高度重叠，summary说明为何仍值得同时保留，证据没有差异则减少同类名额；不为凑满3个方向强行选择。"
    "若无同区间基准数据，不得把绝对上涨称为相对大盘走强；成份股样本不是全体，不得臆测全市场广度、集中度或资金迁移。"
    "relativeStrength按实际startDate/endDate给出5/10个观测交易日相对上证及科创50的收益差，单位百分点；"
    "仅status=available可用，且必须说明实际日期。跑赢一个基准不能说同时跑赢两个，旧截至日不能说今日转强。"
    "riskContext.pairs是共同风险检查：correlation20d为已对齐收益相关性，样本不足或无波动时未知，不能当0；"
    "strongStockSampleOverlap只是所给强势股样本重合，不能推断全成份股重合或组合权重；sharedNewsSources是共同URL，"
    "不同URL仍可能转载同一事件，不能声称为独立证据。低相关也不能证明产业链风险独立。"
    "优先比较有新催化且动量改善/相对强度有支持的方向，并与延续、拥挤和转弱阶段对照；指标不构成经回测验证的硬阈值。"
    "若多个入选方向高度共振，在summary解释共同驱动及为何仍保留；无法说明新增证据差异时减少重复方向。"
    "休市期间价格没有刷新不等于催化延续；重新核验新闻和复市兑现风险，阶段反馈不得当成最终成败。"
    "historicalFeedback.missedLeaderCases是已到期、已核对的同口径漏选对照，仅用于复盘，不是本次候选或推荐标签。"
    "candidateAvailability=outside_pool提示召回缺口，in_pool提示进一步核查排序，unknown不能推断模型当时看过或拒绝过；"
    "prePredictionTechnicalData若为空，禁止用后验涨幅倒造当时信号。"
    "覆盖不足时只称已覆盖范围最强；relatedObservationCount表示重叠观察，不能当作多次独立成功。"
    "旧赢家的后来新闻不得倒灌原预测，历史缺口不记为0。学习可复用条件及其反例，不记住概念名称；"
    "不把东财与同花顺同名指数串成一条收益序列，不声称本次提示词已提高准确率。"
)


def candidate_audit(evidence: dict, selected: set[str]) -> dict:
    """Save what the model could see, including unselected names; never backfill old runs."""
    candidates = []
    for item in evidence["candidates"]:
        candidates.append({
            **{key: deepcopy(item.get(key)) for key in (
                "code", "name", "tradeDate", "pctChg", "change5d", "change10d", "amount", "breadth",
                "mainNetInflow", "technicalData", "candidateSignals", "discoveryFeatures", "eventClues", "relativeStrength", "warnings")},
            "selected": item["code"] in selected,
            "stocks": [{key: stock.get(key) for key in ("code", "name", "pctChg", "tradeDate")} for stock in item.get("stocks", [])],
            "news": [{key: source.get(key) for key in ("id", "title", "publishedAt", "publishedAtPrecision", "temporalRelation", "source", "url")}
                     for source in item.get("evidence", []) if source.get("kind") == "news"],
        })
    return {"version": 1, "complete": True, "decisionAt": evidence.get("decisionAt"),
            "dataAsOf": evidence.get("dataAsOf"), "quoteAsOf": evidence.get("quoteAsOf"),
            "scope": evidence.get("scope"), "selection": deepcopy(evidence.get("candidateSelection")),
            "candidates": candidates,
            "note": "生成时模型候选摘要；selected仅记录最终入选，不是模型评分或完整推理；新闻保留标题与来源用于回查。"}
