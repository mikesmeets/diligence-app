import { useState, useEffect } from 'react';
import Head from 'next/head';
import AddIdeaModal from '../components/AddIdeaModal';
import IdeaTable from '../components/IdeaTable';

const STORAGE_KEY = 'diligence-ideas';

function loadIdeas() {
  if (typeof window === 'undefined') return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveIdeas(ideas) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(ideas));
}

export default function Home() {
  const [ideas, setIdeas] = useState([]);
  const [showModal, setShowModal] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState(null);

  useEffect(() => {
    setIdeas(loadIdeas());
  }, []);

  function updateIdeas(next) {
    setIdeas(next);
    saveIdeas(next);
  }

  function handleAdd(idea) {
    updateIdeas([...ideas, { ...idea, id: Date.now().toString() }]);
    setShowModal(false);
  }

  function handleDelete(id) {
    updateIdeas(ideas.filter(i => i.id !== id));
  }

  async function handleRefresh() {
    setRefreshing(true);
    const updated = await Promise.all(
      ideas.map(async (idea) => {
        if (idea.assetClass !== 'public_equity' || !idea.ticker) return idea;
        try {
          const res = await fetch(`/api/stock-price?ticker=${encodeURIComponent(idea.ticker)}&date=today`);
          const data = await res.json();
          if (data.price != null) return { ...idea, currentPrice: data.price };
        } catch {}
        return idea;
      })
    );
    updateIdeas(updated);
    setLastRefreshed(new Date().toLocaleTimeString());
    setRefreshing(false);
  }

  const publicEquityCount = ideas.filter(i => i.assetClass === 'public_equity').length;

  return (
    <>
      <Head>
        <title>Diligence App</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>

      <div className="min-h-screen bg-gray-50">
        {/* Header */}
        <header className="bg-white border-b border-gray-200 px-6 py-4">
          <div className="max-w-screen-2xl mx-auto flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold text-gray-900 tracking-tight">Diligence App</h1>
              <p className="text-sm text-gray-400 mt-0.5">Investment Idea Tracker</p>
            </div>
            <div className="flex items-center gap-3">
              {lastRefreshed && (
                <span className="text-xs text-gray-400">Updated {lastRefreshed}</span>
              )}
              {publicEquityCount > 0 && (
                <button
                  onClick={handleRefresh}
                  disabled={refreshing}
                  className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50 transition-colors"
                >
                  {refreshing ? 'Refreshing…' : '↻ Refresh Prices'}
                </button>
              )}
              <button
                onClick={() => setShowModal(true)}
                className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors shadow-sm"
              >
                + Add Idea
              </button>
            </div>
          </div>
        </header>

        {/* Stats bar */}
        {ideas.length > 0 && (
          <div className="bg-white border-b border-gray-100 px-6 py-3">
            <div className="max-w-screen-2xl mx-auto flex gap-8 text-sm text-gray-500">
              <span><strong className="text-gray-800">{ideas.length}</strong> ideas</span>
              <span>
                <strong className="text-green-600">{ideas.filter(i => i.direction === 'long').length}</strong> long
              </span>
              <span>
                <strong className="text-red-600">{ideas.filter(i => i.direction === 'short').length}</strong> short
              </span>
            </div>
          </div>
        )}

        {/* Main content */}
        <main className="max-w-screen-2xl mx-auto px-6 py-8">
          <IdeaTable ideas={ideas} onDelete={handleDelete} />
        </main>
      </div>

      {showModal && (
        <AddIdeaModal onAdd={handleAdd} onClose={() => setShowModal(false)} />
      )}
    </>
  );
}
