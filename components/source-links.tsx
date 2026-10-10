import type { ConceptSource } from '@/lib/concept-types';
import { timestamp } from '@/lib/format';

export function SourceLinks({ sources }: { sources: ConceptSource[] }) {
  return sources.map(source => (
    <div className="concept-ai-source" key={source.id}>
      <a href={source.url} target="_blank" rel="noopener noreferrer">
        {source.title} ↗<small>{source.source} · {source.publishedAtPrecision === 'date' ? `${source.publishedAt.slice(0, 10)}（时刻未知）` : timestamp(source.publishedAt)}{source.temporalRelation === 'after_quote' ? ' · 晚于行情快照' : source.temporalRelation === 'unknown' ? ' · 与行情的先后未核实' : ''}</small>
      </a>
      {source.kind === 'news' && <details><summary>查看报道摘录</summary><p>{source.excerpt}</p></details>}
    </div>
  ));
}
