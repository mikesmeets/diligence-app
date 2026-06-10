import { useState } from 'react';

const today = new Date().toISOString().split('T')[0];

const ASSET_CLASSES = [
  { value: 'public_equity', label: 'Public Equity' },
  { value: 'private_equity', label: 'Private Equity' },
  { value: 'bond', label: 'Bond' },
  { value: 'real_estate', label: 'Real Estate' },
];

async function fetchPrice(ticker, date) {
  const res = await fetch(
    `/api/stock-price?ticker=${encodeURIComponent(ticker)}&date=${encodeURIComponent(date)}`
  );
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Failed to fetch price');
  return data.price;
}

export default function AddIdeaModal({ onAdd, onClose }) {
  const [form, setForm] = useState({
    ticker: '',
    ideaDate: today,
    ideaPrice: '',
    initialDate: today,
    initialPrice: '',
    thesis: '',
    direction: 'long',
    assetClass: 'public_equity',
  });
  const [loading, setLoading] = useState({ ideaPrice: false, initialPrice: false, submit: false });
  const [errors, setErrors] = useState({});

  const isPublicEquity = form.assetClass === 'public_equity';

  const set = (field, value) => setForm(f => ({ ...f, [field]: value }));
  const setLoad = (field, value) => setLoading(l => ({ ...l, [field]: value }));
  const clearError = (field) => setErrors(e => { const n = { ...e }; delete n[field]; return n; });

  async function autoFetchPrice(priceField, ticker, date) {
    if (!ticker || !date || !isPublicEquity) return;
    setLoad(priceField, true);
    clearError(priceField);
    try {
      const price = await fetchPrice(ticker, date);
      set(priceField, price.toFixed(2));
    } catch (err) {
      setErrors(e => ({ ...e, [priceField]: err.message }));
    } finally {
      setLoad(priceField, false);
    }
  }

  async function handleTickerBlur() {
    const t = form.ticker.trim();
    if (!t || !isPublicEquity) return;
    await Promise.all([
      autoFetchPrice('ideaPrice', t, form.ideaDate),
      autoFetchPrice('initialPrice', t, form.initialDate),
    ]);
  }

  async function handleSubmit(e) {
    e.preventDefault();
    const newErrors = {};
    if (!form.ticker.trim()) newErrors.ticker = 'Required';
    if (!form.ideaDate) newErrors.ideaDate = 'Required';
    if (!form.initialDate) newErrors.initialDate = 'Required';
    if (!form.thesis.trim()) newErrors.thesis = 'Required';
    if (Object.keys(newErrors).length > 0) { setErrors(newErrors); return; }

    setLoad('submit', true);
    let currentPrice = null;
    if (isPublicEquity && form.ticker) {
      try {
        currentPrice = await fetchPrice(form.ticker.trim(), 'today');
      } catch {}
    }
    setLoad('submit', false);

    onAdd({
      ticker: form.ticker.trim().toUpperCase(),
      ideaDate: form.ideaDate,
      ideaPrice: form.ideaPrice ? parseFloat(form.ideaPrice) : null,
      initialDate: form.initialDate,
      initialPrice: form.initialPrice ? parseFloat(form.initialPrice) : null,
      currentPrice,
      thesis: form.thesis.trim(),
      direction: form.direction,
      assetClass: form.assetClass,
    });
  }

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <div className="px-6 py-4 border-b border-gray-200 flex items-center justify-between sticky top-0 bg-white">
          <h2 className="text-lg font-semibold text-gray-900">Add Investment Idea</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-5 space-y-4">
          {/* Asset Class */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Asset Class</label>
            <select
              value={form.assetClass}
              onChange={e => {
                setForm(f => ({ ...f, assetClass: e.target.value, ideaPrice: '', initialPrice: '' }));
                setErrors({});
              }}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none"
            >
              {ASSET_CLASSES.map(ac => (
                <option key={ac.value} value={ac.value}>{ac.label}</option>
              ))}
            </select>
          </div>

          {/* Ticker + Direction row */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Ticker / Name
              </label>
              <input
                type="text"
                value={form.ticker}
                onChange={e => { set('ticker', e.target.value.toUpperCase()); clearError('ticker'); }}
                onBlur={handleTickerBlur}
                placeholder={isPublicEquity ? 'e.g. AAPL' : 'e.g. Acme Corp'}
                className={`w-full border rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500 ${errors.ticker ? 'border-red-400' : 'border-gray-300'}`}
              />
              {errors.ticker && <p className="text-red-500 text-xs mt-1">{errors.ticker}</p>}
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Direction</label>
              <div className="flex gap-4 mt-2.5">
                {['long', 'short'].map(dir => (
                  <label key={dir} className="flex items-center gap-1.5 cursor-pointer select-none">
                    <input
                      type="radio"
                      name="direction"
                      value={dir}
                      checked={form.direction === dir}
                      onChange={() => set('direction', dir)}
                      className="accent-blue-600"
                    />
                    <span className="text-sm capitalize font-medium">{dir}</span>
                  </label>
                ))}
              </div>
            </div>
          </div>

          {/* Idea Date + Price */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Idea Date</label>
              <input
                type="date"
                value={form.ideaDate}
                onChange={e => { set('ideaDate', e.target.value); clearError('ideaDate'); }}
                onBlur={() => autoFetchPrice('ideaPrice', form.ticker.trim(), form.ideaDate)}
                className={`w-full border rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500 ${errors.ideaDate ? 'border-red-400' : 'border-gray-300'}`}
              />
              {errors.ideaDate && <p className="text-red-500 text-xs mt-1">{errors.ideaDate}</p>}
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Price on Idea Date
                {loading.ideaPrice && <span className="text-blue-500 ml-1 text-xs">fetching...</span>}
              </label>
              <input
                type="number"
                step="0.01"
                min="0"
                value={form.ideaPrice}
                onChange={e => set('ideaPrice', e.target.value)}
                placeholder={isPublicEquity ? 'Auto-fetched' : 'Optional'}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500"
              />
              {errors.ideaPrice && <p className="text-red-500 text-xs mt-1">{errors.ideaPrice}</p>}
            </div>
          </div>

          {/* Initial Date + Price */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Initial Tracking Date</label>
              <input
                type="date"
                value={form.initialDate}
                onChange={e => { set('initialDate', e.target.value); clearError('initialDate'); }}
                onBlur={() => autoFetchPrice('initialPrice', form.ticker.trim(), form.initialDate)}
                className={`w-full border rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500 ${errors.initialDate ? 'border-red-400' : 'border-gray-300'}`}
              />
              {errors.initialDate && <p className="text-red-500 text-xs mt-1">{errors.initialDate}</p>}
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Price on Initial Date
                {loading.initialPrice && <span className="text-blue-500 ml-1 text-xs">fetching...</span>}
              </label>
              <input
                type="number"
                step="0.01"
                min="0"
                value={form.initialPrice}
                onChange={e => set('initialPrice', e.target.value)}
                placeholder={isPublicEquity ? 'Auto-fetched' : 'Optional'}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500"
              />
              {errors.initialPrice && <p className="text-red-500 text-xs mt-1">{errors.initialPrice}</p>}
            </div>
          </div>

          {/* Thesis */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Investment Thesis</label>
            <textarea
              value={form.thesis}
              onChange={e => { set('thesis', e.target.value); clearError('thesis'); }}
              rows={3}
              placeholder="Describe the investment thesis..."
              className={`w-full border rounded-lg px-3 py-2 text-sm resize-none outline-none focus:ring-2 focus:ring-blue-500 ${errors.thesis ? 'border-red-400' : 'border-gray-300'}`}
            />
            {errors.thesis && <p className="text-red-500 text-xs mt-1">{errors.thesis}</p>}
          </div>

          <div className="flex gap-3 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading.submit}
              className="flex-1 px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-60 transition-colors"
            >
              {loading.submit ? 'Saving...' : 'Add Idea'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
