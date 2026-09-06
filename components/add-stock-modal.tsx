import { marketLabel } from '@/lib/format';
import type { StockBasic } from '@/lib/types';


type AddStockModalProps = {
  query: string;
  searching: boolean;
  adding: boolean;
  results: StockBasic[];
  onQueryChange: (value: string) => void;
  onAdd: (stock: StockBasic) => void;
  onClose: () => void;
};

export function AddStockModal({
  query,
  searching,
  adding,
  results,
  onQueryChange,
  onAdd,
  onClose,
}: AddStockModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="modal-dialog"
      aria-labelledby="add-title"
      onCancel={(event) => { event.preventDefault(); onClose(); }}
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}
    >
      <section
        className="modal"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-head">
          <div><p className="eyebrow">搜索全部正常上市 A 股</p><h2 id="add-title">添加自选股</h2></div>
          <button onClick={onClose} aria-label="关闭">×</button>
        </div>
        <label className="modal-search">
          <span>⌕</span>
          <input
            autoFocus
            aria-label="搜索股票代码、名称或行业"
            maxLength={30}
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="输入6位代码、名称或行业"
          />
        </label>
        <div className="catalog-list">
          {searching ? (
            <div className="modal-empty"><span className="loading-ring" /> 正在搜索</div>
          ) : results.length ? results.map((stock) => (
            <div key={stock.tsCode}>
              <span className="market-tag">{marketLabel(stock.exchange)}</span>
              <div><strong>{stock.name}</strong><small>{stock.symbol} · {stock.industry ?? '未分类'}</small></div>
              <span />
              <button onClick={() => onAdd(stock)} disabled={adding}>{adding ? '稍候' : '添加'}</button>
            </div>
          )) : (
            <div className="modal-empty">{query ? '没有匹配结果' : '输入代码或名称开始搜索'}</div>
          )}
        </div>
        <p className="modal-footnote">股票列表每日同步一次；自选股保存在本机 data/stockviewer.sqlite3。</p>
      </section>
    </dialog>
  );
}
import { useEffect, useRef } from 'react';
