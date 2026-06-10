import yahooFinance from 'yahoo-finance2';

yahooFinance.suppressNotices(['yahooSurvey']);

export default async function handler(req, res) {
  const { ticker, date } = req.query;

  if (!ticker) {
    return res.status(400).json({ error: 'Ticker is required' });
  }

  const symbol = ticker.toUpperCase();

  try {
    if (!date || date === 'today') {
      const quote = await yahooFinance.quote(symbol, {}, { validateResult: false });
      if (!quote || quote.regularMarketPrice == null) {
        return res.status(404).json({ error: `No current quote found for ${symbol}` });
      }
      return res.json({ price: quote.regularMarketPrice, ticker: symbol });
    }

    // Historical: search a window around the target date to handle weekends/holidays
    const targetDate = new Date(date + 'T12:00:00Z');
    const startDate = new Date(targetDate);
    startDate.setDate(startDate.getDate() - 7);
    const endDate = new Date(targetDate);
    endDate.setDate(endDate.getDate() + 4);

    const historical = await yahooFinance.historical(
      symbol,
      {
        period1: startDate.toISOString().split('T')[0],
        period2: endDate.toISOString().split('T')[0],
        interval: '1d',
      },
      { validateResult: false }
    );

    if (!historical || historical.length === 0) {
      return res.status(404).json({ error: `No historical data for ${symbol} around ${date}` });
    }

    const target = targetDate.getTime();
    const closest = historical.reduce((prev, curr) => {
      const prevDiff = Math.abs(new Date(prev.date).getTime() - target);
      const currDiff = Math.abs(new Date(curr.date).getTime() - target);
      return currDiff < prevDiff ? curr : prev;
    });

    return res.json({
      price: closest.close,
      date: new Date(closest.date).toISOString().split('T')[0],
      ticker: symbol,
    });
  } catch (err) {
    return res.status(500).json({ error: err.message || 'Failed to fetch price' });
  }
}
