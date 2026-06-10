const ASSET_CLASS_LABELS = {
  public_equity: 'Public Equity',
  private_equity: 'Private Equity',
  bond: 'Bond',
  real_estate: 'Real Estate',
};

function fmt(price) {
  if (price == null) return '—';
  return `$${Number(price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function ChangeCell({ from, to, direction }) {
  if (from == null || to == null) return <span className="text-gray-400 text-sm">—</span>;
  const pct = ((to - from) / from) * 100;
  // For longs: up is good. For shorts: down is good.
  const profitable = direction === 'long' ? pct >= 0 : pct <= 0;
  const colorClass = profitable ? 'text-green-600' : 'text-red-600';
  return (
    <span className={`font-medium text-sm ${colorClass}`}>
      {pct >= 0 ? '+' : ''}{pct.toFixed(2)}%
    </span>
  );
}

export default function IdeaTable({ ideas, onDelete }) {
  if (ideas.length === 0) {
    return (
      <div className="text-center py-20 text-gray-400">
        <div className="text-5xl mb-4">📋</div>
        <p className="text-lg font-medium text-gray-500">No ideas yet</p>
        <p className="text-sm mt-1">Click &quot;Add Idea&quot; to start tracking investments.</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-gray-200 shadow-sm">
      <table className="min-w-full bg-white text-sm">
        <thead>
          <tr className="bg-gray-50 border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
            <th className="px-4 py-3 text-left font-semibold">Ticker</th>
            <th className="px-4 py-3 text-left font-semibold">Dir</th>
            <th className="px-4 py-3 text-left font-semibold">Asset Class</th>
            <th className="px-4 py-3 text-left font-semibold">Idea Date</th>
            <th className="px-4 py-3 text-right font-semibold">Idea Price</th>
            <th className="px-4 py-3 text-left font-semibold">Initial Date</th>
            <th className="px-4 py-3 text-right font-semibold">Initial Price</th>
            <th className="px-4 py-3 text-right font-semibold">Current Price</th>
            <th className="px-4 py-3 text-right font-semibold">Idea → Initial</th>
            <th className="px-4 py-3 text-right font-semibold">Initial → Today</th>
            <th className="px-4 py-3 text-left font-semibold">Thesis</th>
            <th className="px-4 py-3"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {ideas.map((idea) => (
            <tr key={idea.id} className="hover:bg-blue-50 transition-colors group">
              <td className="px-4 py-3 font-bold text-gray-900">{idea.ticker}</td>
              <td className="px-4 py-3">
                <span
                  className={`inline-block px-2 py-0.5 rounded-full text-xs font-semibold ${
                    idea.direction === 'long'
                      ? 'bg-green-100 text-green-700'
                      : 'bg-red-100 text-red-700'
                  }`}
                >
                  {idea.direction.toUpperCase()}
                </span>
              </td>
              <td className="px-4 py-3 text-gray-600 whitespace-nowrap">
                {ASSET_CLASS_LABELS[idea.assetClass] || idea.assetClass}
              </td>
              <td className="px-4 py-3 text-gray-600 whitespace-nowrap">{idea.ideaDate}</td>
              <td className="px-4 py-3 text-right text-gray-900 font-mono">{fmt(idea.ideaPrice)}</td>
              <td className="px-4 py-3 text-gray-600 whitespace-nowrap">{idea.initialDate}</td>
              <td className="px-4 py-3 text-right text-gray-900 font-mono">{fmt(idea.initialPrice)}</td>
              <td className="px-4 py-3 text-right text-gray-900 font-mono">{fmt(idea.currentPrice)}</td>
              <td className="px-4 py-3 text-right">
                <ChangeCell from={idea.ideaPrice} to={idea.initialPrice} direction={idea.direction} />
              </td>
              <td className="px-4 py-3 text-right">
                <ChangeCell from={idea.initialPrice} to={idea.currentPrice} direction={idea.direction} />
              </td>
              <td className="px-4 py-3 text-gray-600 max-w-xs">
                <p
                  className="truncate"
                  title={idea.thesis}
                >
                  {idea.thesis}
                </p>
              </td>
              <td className="px-4 py-3">
                <button
                  onClick={() => {
                    if (confirm(`Delete idea for ${idea.ticker}?`)) onDelete(idea.id);
                  }}
                  className="text-gray-300 group-hover:text-gray-400 hover:!text-red-500 text-xs transition-colors"
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
