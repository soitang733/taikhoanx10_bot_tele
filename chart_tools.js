"use strict";

// Indicator calculations are pure so they can be tested without a browser.
(function (root) {
  function sma(values, period) {
    const result = [];
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
      sum += values[i];
      if (i >= period) sum -= values[i - period];
      if (i >= period - 1) result.push({ index: i, value: sum / period });
    }
    return result;
  }

  function bollinger(values, period = 20, multiplier = 2) {
    const middle = sma(values, period);
    return middle.map(({ index, value }) => {
      const window = values.slice(index - period + 1, index + 1);
      const variance = window.reduce((total, point) => total + (point - value) ** 2, 0) / period;
      const deviation = multiplier * Math.sqrt(variance);
      return { index, middle: value, upper: value + deviation, lower: value - deviation };
    });
  }

  function rsi(values, period = 14) {
    if (values.length <= period) return [];
    let gain = 0, loss = 0;
    for (let i = 1; i <= period; i++) {
      const delta = values[i] - values[i - 1];
      gain += Math.max(delta, 0);
      loss += Math.max(-delta, 0);
    }
    gain /= period;
    loss /= period;
    const output = [];
    const level = () => loss === 0 ? (gain === 0 ? 50 : 100) : 100 - 100 / (1 + gain / loss);
    output.push({ index: period, value: level() });
    for (let i = period + 1; i < values.length; i++) {
      const delta = values[i] - values[i - 1];
      gain = (gain * (period - 1) + Math.max(delta, 0)) / period;
      loss = (loss * (period - 1) + Math.max(-delta, 0)) / period;
      output.push({ index: i, value: level() });
    }
    return output;
  }

  function adjustedQuotePrice(rawPrice, adjustedClose, rawClose) {
    const values = [rawPrice, adjustedClose, rawClose].map(Number);
    return values.every(value => Number.isFinite(value) && value > 0)
      ? values[0] * values[1] / values[2] : null;
  }

  function normalizeSessionCandle(candle, lastEod) {
    if (!candle || !lastEod || String(candle.date) <= lastEod.time) return null;
    const factor = adjustedQuotePrice(1, lastEod.close, lastEod.rawClose);
    const values = ["open", "high", "low", "close", "volume"].map(key => Number(candle[key]));
    if (factor === null || !values.every(Number.isFinite) || values.slice(0, 4).some(value => value <= 0)
        || values[4] < 0 || values[1] < Math.max(values[0], values[2], values[3])
        || values[2] > Math.min(values[0], values[1], values[3])) return null;
    return {
      time: String(candle.date).slice(0, 10),
      open: values[0] * factor, high: values[1] * factor,
      low: values[2] * factor, close: values[3] * factor,
      volume: values[4], rawClose: values[3], provisional: true,
    };
  }

  function create(container, ticker, rows, indicatorState, onHint) {
    if (!root.LightweightCharts) throw new Error("Chưa tải được thư viện biểu đồ");
    const library = root.LightweightCharts;
    const candlesData = rows.map(row => ({
      time: String(row.date).slice(0, 10),
      open: Number(row.adjusted_open ?? row.open),
      high: Number(row.adjusted_high ?? row.high),
      low: Number(row.adjusted_low ?? row.low),
      close: Number(row.adjusted_close ?? row.close),
      rawClose: Number(row.close),
      volume: Number(row.volume) || 0,
    })).filter(row => [row.open, row.high, row.low, row.close].every(Number.isFinite));
    if (candlesData.length < 2) throw new Error("Chưa đủ nến giá để vẽ biểu đồ");
    const lastEod = candlesData[candlesData.length - 1];
    const chart = library.createChart(container, {
      width: container.clientWidth, height: container.clientHeight,
      layout: { background: { type: "solid", color: "#ffffff" }, textColor: "#60748a", attributionLogo: false },
      grid: { vertLines: { color: "#edf2f7" }, horzLines: { color: "#edf2f7" } },
      rightPriceScale: { borderColor: "#dfe8f2" },
      timeScale: { borderColor: "#dfe8f2" },
    });
    const candles = chart.addSeries(library.CandlestickSeries, {
      upColor: "#0fa987", downColor: "#dc4d5c", borderVisible: false,
      wickUpColor: "#0fa987", wickDownColor: "#dc4d5c",
      priceFormat: { type: "price", precision: 0, minMove: 100 },
    });
    candles.setData(candlesData.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    const volume = chart.addSeries(library.HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "" });
    volume.priceScale().applyOptions({ scaleMargins: { top: .78, bottom: 0 } });
    volume.setData(candlesData.map(row => ({ time: row.time, value: row.volume,
      color: row.close >= row.open ? "#0fa98755" : "#dc4d5c55" })));
    chart.timeScale().fitContent();

    const closes = candlesData.map(row => row.close);
    const studies = [];
    let currentIndicators = { ...indicatorState };
    function clearStudies() { while (studies.length) chart.removeSeries(studies.pop()); }
    function addLine(points, color, paneIndex = 0, width = 2) {
      const series = chart.addSeries(library.LineSeries, { color, lineWidth: width,
        priceLineVisible: false, lastValueVisible: false }, paneIndex);
      series.setData(points);
      studies.push(series);
      return series;
    }
    function indicators(state) {
      currentIndicators = { ...state };
      clearStudies();
      if (state.ma) {
        [[20, "#efa82c"], [50, "#3888d6"], [200, "#965cc7"]].forEach(([period, color]) => {
          addLine(sma(closes, period).map(p => ({ time: candlesData[p.index].time, value: p.value })), color);
        });
      }
      if (state.bb) {
        const bands = bollinger(closes);
        addLine(bands.map(p => ({ time: candlesData[p.index].time, value: p.upper })), "#8292b5", 0, 1);
        addLine(bands.map(p => ({ time: candlesData[p.index].time, value: p.lower })), "#8292b5", 0, 1);
      }
      if (state.rsi) {
        const points = rsi(closes).map(p => ({ time: candlesData[p.index].time, value: p.value }));
        if (points.length) addLine(points, "#a06cd5", 1);
      }
    }
    indicators(indicatorState);

    let flashTimer = null;
    function updateSessionCandle(candle) {
      const bar = normalizeSessionCandle(candle, lastEod);
      if (!bar) return null;
      const last = candlesData[candlesData.length - 1];
      const changed = last.time !== bar.time ||
        ["open", "high", "low", "close", "volume"].some(key => last[key] !== bar[key]);
      if (!changed) return { ...bar, changed: false };
      if (last.time === bar.time) {
        candlesData[candlesData.length - 1] = bar;
        closes[closes.length - 1] = bar.close;
      } else {
        candlesData.push(bar);
        closes.push(bar.close);
      }
      const normalColor = bar.close >= bar.open ? "#0fa987" : "#dc4d5c";
      const canFlash = !root.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      const data = { time: bar.time, open: bar.open, high: bar.high,
                     low: bar.low, close: bar.close };
      if (flashTimer) root.clearTimeout(flashTimer);
      candles.update({ ...data, color: canFlash ? "#f2b12e" : normalColor,
                       wickColor: canFlash ? "#f2b12e" : normalColor });
      if (canFlash) flashTimer = root.setTimeout(() => {
        candles.update({ ...data, color: normalColor, wickColor: normalColor });
        flashTimer = null;
      }, 900);
      volume.update({ time: bar.time, value: bar.volume,
                      color: bar.close >= bar.open ? "#0fa98755" : "#dc4d5c55" });
      indicators(currentIndicators);
      return { ...bar, changed: true };
    }

    const storageKey = `x10-drawings-${ticker}`;
    let drawings = [];
    try { drawings = JSON.parse(root.localStorage.getItem(storageKey) || "[]"); }
    catch (_) { drawings = []; }
    if (!Array.isArray(drawings)) drawings = [];
    let rendered = [];
    let mode = "", firstPoint = null;
    function save() { try { root.localStorage.setItem(storageKey, JSON.stringify(drawings)); } catch (_) {} }
    function redraw() {
      rendered.forEach(item => item.kind === "line" ? chart.removeSeries(item.ref) : candles.removePriceLine(item.ref));
      rendered = [];
      for (const drawing of drawings) {
        if (drawing.kind === "horizontal") {
          const ref = candles.createPriceLine({ price: drawing.price, color: "#f2a626", lineWidth: 2,
            lineStyle: library.LineStyle.Dashed, axisLabelVisible: true, title: "Mốc vẽ" });
          rendered.push({ kind: "horizontal", ref });
        } else if (drawing.kind === "trend" && drawing.a.time !== drawing.b.time) {
          const ref = chart.addSeries(library.LineSeries, { color: "#ef8e22", lineWidth: 2,
            priceLineVisible: false, lastValueVisible: false });
          ref.setData([drawing.a, drawing.b].sort((a, b) => a.time.localeCompare(b.time)));
          rendered.push({ kind: "line", ref });
        }
      }
    }
    redraw();
    let quoteLine = null;
    function updateQuote(quote) {
      if (quoteLine) {
        candles.removePriceLine(quoteLine);
        quoteLine = null;
      }
      if (!quote?.fresh || !Number.isFinite(Number(quote.price_vnd))) return null;
      // DNSE latest trade is raw VND; candles are back-adjusted EOD.
      // Map the quote to the chart scale using the latest EOD adjustment ratio.
      const last = candlesData[candlesData.length - 1];
      const rawClose = last.rawClose;
      const adjustedQuote = adjustedQuotePrice(quote.price_vnd, last.close, rawClose);
      if (adjustedQuote === null) return null;
      quoteLine = candles.createPriceLine({
        price: adjustedQuote, color: "#0b9e80", lineWidth: 2,
        lineStyle: library.LineStyle.Solid, axisLabelVisible: true,
        title: "Khớp mới*",
      });
      return adjustedQuote;
    }
    chart.subscribeClick(param => {
      if (!mode || !param.point || !param.time) return;
      const price = candles.coordinateToPrice(param.point.y);
      if (!Number.isFinite(price)) return;
      const time = typeof param.time === "string" ? param.time :
        `${param.time.year}-${String(param.time.month).padStart(2, "0")}-${String(param.time.day).padStart(2, "0")}`;
      if (mode === "horizontal") {
        drawings.push({ kind: "horizontal", price: Math.round(price) });
        onHint("Đã vẽ mốc giá ngang. Chạm tiếp để thêm mốc khác.");
      } else if (!firstPoint) {
        firstPoint = { time, value: price };
        onHint("Chọn điểm thứ hai cho đường xu hướng.");
        return;
      } else {
        drawings.push({ kind: "trend", a: firstPoint, b: { time, value: price } });
        firstPoint = null;
        onHint("Đã vẽ đường xu hướng. Chạm tiếp để vẽ đường mới.");
      }
      save(); redraw();
    });
    const observer = new ResizeObserver(() => chart.applyOptions({ width: container.clientWidth, height: container.clientHeight }));
    observer.observe(container);
    return {
      indicators, updateQuote, updateSessionCandle,
      setMode(next) { mode = mode === next ? "" : next; firstPoint = null;
        onHint(mode === "trend" ? "Chạm 2 điểm trên biểu đồ để vẽ đường xu hướng." :
          mode === "horizontal" ? "Chạm mức giá cần đánh dấu." : "Đã tắt công cụ vẽ."); return mode; },
      undo() { drawings.pop(); save(); redraw(); onHint("Đã hoàn tác nét vẽ gần nhất."); },
      clear() { drawings = []; firstPoint = null; save(); redraw(); onHint("Đã xóa nét vẽ của mã này."); },
      destroy() { if (flashTimer) root.clearTimeout(flashTimer); observer.disconnect(); chart.remove(); },
    };
  }

  const api = { sma, bollinger, rsi, adjustedQuotePrice, normalizeSessionCandle, create };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ChartTools = api;
})(typeof window !== "undefined" ? window : globalThis);
