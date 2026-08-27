import type { AiProvider, StrategyId } from '@/lib/types';


export type StrategyMeta = {
  fallbackName: string;
  index: string;
  color: string;
  tags: string[];
  source: string;
  url: string | null;
};

export const PUBLIC_STRATEGY_META = {
  dividend: {
    fallbackName: '红利低波', index: '公开策略 01', color: 'forest',
    tags: ['估值约束', '低波动', '红利数据可用时纳入'],
    source: '上海证券交易所 · 上证红利质量指数方法（借鉴）',
    url: 'https://www.sse.com.cn/market/sseindex/diclosure/c/c_20250123_10770595.shtml',
  },
  momentum: {
    fallbackName: '价格动量', index: '公开策略 02', color: 'clay',
    tags: ['近半年动量', '近一年动量', '成交流动性'],
    source: '国证指数 · 创业板动量成长指数方法',
    url: 'https://www.cnindex.com.cn/docs/gz_399296.pdf',
  },
  lowvol: {
    fallbackName: '小盘低波', index: '公开策略 03', color: 'indigo',
    tags: ['小盘暴露', '一年波动率', '流动性筛选'],
    source: '国证指数 · 巨潮小盘低波指数方法',
    url: 'https://www.cnindex.com.cn/docs/gz_399408.pdf',
  },
  qlib_alpha158: {
    fallbackName: 'Qlib 精简多因子', index: 'GitHub 策略 04', color: 'cobalt',
    tags: ['ROC / MA / STD', '5/20/60日窗口', '价格与量能'],
    source: 'GitHub · microsoft/qlib · Alpha158 数据加载器',
    url: 'https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py',
  },
  sma_cross: {
    fallbackName: '双均线趋势', index: 'GitHub 策略 05', color: 'rust',
    tags: ['现价 > MA10 > MA30', '20日收益为正', '均线金叉'],
    source: 'GitHub · kernc/backtesting.py · SmaCross 示例',
    url: 'https://github.com/kernc/backtesting.py/blob/master/doc/examples/Strategies%20Library.py',
  },
  rsi_rebound: {
    fallbackName: 'RSI 超跌回升', index: 'GitHub 策略 06', color: 'sage',
    tags: ['Wilder RSI14', '三日回升', '严格条件不补位'],
    source: 'GitHub · QuantConnect/Lean · RsiAlphaModel',
    url: 'https://github.com/QuantConnect/Lean/blob/master/Algorithm.Framework/Alphas/RsiAlphaModel.py',
  },
  trend_confirmation: {
    fallbackName: '趋势确认', index: '量化策略 07', color: 'teal',
    tags: ['现价 > MA20 > MA60', '20/60日动量为正', '低波与流动性'],
    source: '国证指数 · 动量成长方法（趋势因子借鉴）',
    url: 'https://www.cnindex.com.cn/docs/gz_399296.pdf',
  },
  value_momentum: {
    fallbackName: '价值动量', index: '量化策略 08', color: 'plum',
    tags: ['正 PE / PB', '60日与半年动量', '成交流动性'],
    source: '国证指数 · AlphaFocus 单因子指数系列',
    url: 'https://www.cnindex.com.cn/zh_information/notices_news/2021/202105/t20210525_17317.html?act_menu=2',
  },
  volume_breakout: {
    fallbackName: '强势缩量筛选', index: '自定义策略 09', color: 'ochre',
    tags: ['10日涨幅 > 30%', '20日均额 > 12亿', '量比 < 1.5'],
    source: '用户自定义严格筛选条件', url: null,
  },
  hot_concept: {
    fallbackName: '热门概念共振', index: '市场策略 10', color: 'rose',
    tags: ['概念涨幅', '上涨家数占比', '个股成交额'],
    source: 'AKShare · 东方财富概念板块实时行情与成份股',
    url: 'https://akshare.akfamily.xyz/data/stock/stock.html',
  },
} satisfies Record<StrategyId, StrategyMeta>;

export const AI_META = {
  deepseek: {
    model: 'DeepSeek', monogram: 'D', theme: 'deepseek', fallbackTitle: '统一研究排序',
    logic: '同一提示语 · 同一候选池 · 同一输出约束',
  },
  gemini: {
    model: 'Gemini', monogram: 'G', theme: 'gemini', fallbackTitle: '统一研究排序',
    logic: '同一提示语 · 同一候选池 · 同一输出约束',
  },
  openai: {
    model: 'ChatGPT', monogram: 'C', theme: 'chatgpt', fallbackTitle: '统一研究排序',
    logic: '同一提示语 · 同一候选池 · 同一输出约束',
  },
} satisfies Record<AiProvider, {
  model: string;
  monogram: string;
  theme: string;
  fallbackTitle: string;
  logic: string;
}>;
