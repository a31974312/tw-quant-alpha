/* ==========================================================================
   TW-Quant Alpha — 台股全市場即時量化交易與條件警示前端引擎
   ========================================================================== */

(() => {
  "use strict";

  const STATE = {
    mode: "LIVE",
    marketOpen: false,
    soundEnabled: true,
    stocks: [],
    stockMap: new Map(),
    indices: {},
    breadth: {},
    rules: [],
    ruleHitCounts: {},
    customPriceAlerts: [],
    alerts: [],
    totalAlertCount: 0,

    selectedCode: "2330",
    selectedDetail: null,
    klineInterval: "1d",
    klineBarCount: 55,
    subChartType: "VOL",
    hoverIndex: null,

    alertCategoryFilter: "ALL",
    screenerMarket: "ALL",
    screenerQuery: "",
    sortKey: "turnoverM",
    sortDir: "desc",
    page: 1,
    pageSize: 50,

    watchlist: new Set(
      (() => {
        try {
          return JSON.parse(window.localStorage.getItem("tw_quant_watchlist") || '["2330","2317","2454","2603","6488","0050"]');
        } catch (_) {
          return ["2330", "2317", "2454", "2603", "6488", "0050"];
        }
      })()
    ),
    searchHighlightIdx: 0,
    isSearchComposing: false,
    isTableComposing: false,
    searchFetchSeq: 0,
    audioCtx: null,
  };

  // ==================== 格式化工具 ====================
  const fmtNum = (v, digits = 2) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
    return Number(v).toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  };

  const fmtPrice = (v) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
    const n = Number(v);
    if (n >= 500) return fmtNum(n, n % 1 === 0 ? 0 : 1);
    if (n >= 100) return fmtNum(n, n % 1 === 0 ? 1 : 2);
    return fmtNum(n, 2);
  };

  const fmtInt = (v) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
    return Math.round(Number(v)).toLocaleString("en-US");
  };

  const fmtSigned = (v, digits = 2) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
    const n = Number(v);
    const sign = n > 0 ? "+" : "";
    return `${sign}${fmtNum(n, digits)}`;
  };

  const priceColorClass = (chg) => {
    if (!chg) return "flat-text";
    return chg > 0 ? "up-text" : chg < 0 ? "down-text" : "flat-text";
  };

  const formatRocDate = (rocStr) => {
    if (!rocStr) return "--";
    const s = String(rocStr).trim();
    if (s.length === 7) {
      const y = parseInt(s.slice(0, 3), 10) + 1911;
      return `${y}-${s.slice(3, 5)}-${s.slice(5, 7)}`;
    }
    if (s.length === 8) {
      return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
    }
    return s;
  };

  const escapeHtml = (str) =>
    String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  // iOS / Android 繁體中文注音符號（尚未選字確認之注音碼）偵測與台股字元正規化
  const ZHUYIN_RE = /[\u3105-\u3129\u02CA\u02C7\u02CB\u02D9]+/g;
  const HAS_ZHUYIN_RE = /[\u3105-\u3129\u02CA\u02C7\u02CB\u02D9]/;

  const normalizeSearch = (raw) => {
    if (!raw) return "";
    let out = "";
    for (const ch of String(raw)) {
      const code = ch.charCodeAt(0);
      if (code === 0x3000) {
        out += " ";
      } else if (code >= 0xff01 && code <= 0xff5e) {
        out += String.fromCharCode(code - 0xfee0);
      } else {
        out += ch;
      }
    }
    return out.replace(/臺/g, "台").toUpperCase().replace(/\s+/g, "");
  };

  const stripUncommittedZhuyin = (raw) => normalizeSearch(String(raw || "").replace(ZHUYIN_RE, ""));

  // ==================== 音效合成器 (Web Audio API) ====================
  function playAlertChime(isRisk = false) {
    if (!STATE.soundEnabled) return;
    try {
      if (!STATE.audioCtx) {
        const AudioContextCls = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextCls) return;
        STATE.audioCtx = new AudioContextCls();
      }
      if (STATE.audioCtx.state === "suspended") {
        STATE.audioCtx.resume();
      }
      const ctx = STATE.audioCtx;
      const now = ctx.currentTime;
      const freqs = isRisk ? [523.25, 392.0] : [659.25, 880.0, 1046.5];
      freqs.forEach((f, idx) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = isRisk ? "triangle" : "sine";
        osc.frequency.setValueAtTime(f, now + idx * 0.09);
        gain.gain.setValueAtTime(0.001, now + idx * 0.09);
        gain.gain.exponentialRampToValueAtTime(0.08, now + idx * 0.09 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + idx * 0.09 + 0.26);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now + idx * 0.09);
        osc.stop(now + idx * 0.09 + 0.28);
      });
    } catch (_) {
      // 瀏覽器尚未互動前自動靜音略過
    }
  }

  // ==================== 頁面即時彈窗通知 (Toast) ====================
  function pushToastAlert(alertObj) {
    const container = document.getElementById("toastContainer");
    if (!container || !alertObj) return;

    const el = document.createElement("div");
    el.className = `toast-alert cat-${escapeHtml(alertObj.category || "BULL")}`;
    const pctStr = fmtSigned(alertObj.changePct, 2) + "%";
    const cls = priceColorClass(alertObj.changePct);

    el.innerHTML = `
      <div class="toast-head">
        <span>🚨 ${escapeHtml(alertObj.badge)}｜${escapeHtml(alertObj.code)} ${escapeHtml(alertObj.name)}</span>
        <button class="toast-close" aria-label="關閉">✕</button>
      </div>
      <div class="toast-body">
        <div class="mono" style="margin-bottom:3px;">
          現在股價：<strong>${fmtPrice(alertObj.price)} 元</strong>
          <span class="${cls}">(${pctStr})</span>
        </div>
        <div>${escapeHtml(alertObj.reason)}</div>
      </div>
    `;

    el.addEventListener("click", (e) => {
      if (e.target.closest(".toast-close")) {
        el.remove();
        return;
      }
      selectStock(alertObj.code, true);
      el.remove();
    });

    container.prepend(el);
    while (container.children.length > 4) {
      container.lastElementChild.remove();
    }
    setTimeout(() => {
      if (el.parentNode) el.remove();
    }, 7500);
  }

  // ==================== 初始化與資料同步 ====================
  async function loadBootstrap(showInitialNotice = false) {
    try {
      const res = await fetch("/api/bootstrap");
      const data = await res.json();
      applyBootstrapData(data);
      if (showInitialNotice && STATE.alerts.length > 0) {
        const top = STATE.alerts[0];
        pushToastAlert({
          ...top,
          badge: `全市場掃描完成 (${STATE.totalAlertCount}筆觸發)`,
        });
      }
    } catch (err) {
      console.error("Bootstrap error:", err);
      document.getElementById("connStatusText").textContent = "連線重試中...";
    }
  }

  function applyBootstrapData(data) {
    STATE.mode = data.mode || "LIVE";
    STATE.marketOpen = Boolean(data.marketOpen);
    STATE.indices = data.indices || {};
    STATE.breadth = data.breadth || {};
    STATE.rules = data.rules || [];
    STATE.ruleHitCounts = data.ruleHitCounts || {};
    STATE.customPriceAlerts = data.customPriceAlerts || [];
    STATE.alerts = data.alerts || [];
    STATE.totalAlertCount = data.totalAlertCount || STATE.alerts.length;

    STATE.stocks = data.stocks || [];
    STATE.stockMap.clear();
    for (const st of STATE.stocks) {
      STATE.stockMap.set(st.code, st);
    }

    renderHeaderAndPulse(data);
    renderRulesConfig();
    renderCustomAlertsList();
    renderAlertRadar();
    renderScreenerCounts();
    renderScreenerTable();
  }

  // ==================== 頂部狀態與大盤廣度渲染 ====================
  function renderHeaderAndPulse(meta = {}) {
    const b = STATE.breadth;
    const subEl = document.getElementById("universeSubText");
    if (b && b.total) {
      subEl.textContent = `涵蓋上市 ${fmtInt(b.twseCount)} 檔 · 上櫃 ${fmtInt(b.tpexCount)} 檔 · 興櫃 ${fmtInt(b.esbCount)} 檔（共 ${fmtInt(b.total)} 檔全台股即時監控）`;
    }

    const pill = document.getElementById("connStatusPill");
    const connTxt = document.getElementById("connStatusText");
    const simBtn = document.getElementById("simToggleBtn");
    const syncTime = meta.lastQuoteSync || "--:--:--";

    if (STATE.mode === "SIM") {
      pill.classList.add("sim-active");
      connTxt.textContent = `⚡ 模擬盤中撮合跳動中 (${syncTime})`;
      simBtn.classList.add("sim-on");
      simBtn.textContent = "⏹ 停止模擬 (切回真實報價)";
    } else {
      pill.classList.remove("sim-active");
      const mktTxt = STATE.marketOpen ? "證交所盤中即時連線" : "證交所/櫃買真實收盤連線";
      connTxt.textContent = `${mktTxt} (${syncTime})`;
      simBtn.classList.remove("sim-on");
      simBtn.textContent = "⚡ 模擬盤中跳動";
    }

    if (meta.dataDate) {
      document.getElementById("dataDateLabel").textContent = `交易日 ${formatRocDate(meta.dataDate)}`;
    }

    // 指數渲染
    renderIndexCard("TAIEX", "taiex");
    renderIndexCard("TPEX", "tpex");

    // 漲跌家數
    if (b && b.total) {
      const tot = Math.max(1, (b.up || 0) + (b.flat || 0) + (b.down || 0));
      document.getElementById("breadthUpBar").style.width = `${((b.up || 0) / tot) * 100}%`;
      document.getElementById("breadthFlatBar").style.width = `${((b.flat || 0) / tot) * 100}%`;
      document.getElementById("breadthDownBar").style.width = `${((b.down || 0) / tot) * 100}%`;

      document.getElementById("bUpCount").textContent = fmtInt(b.up || 0);
      document.getElementById("bLuCount").textContent = fmtInt(b.limitUp || 0);
      document.getElementById("bFlatCount").textContent = fmtInt(b.flat || 0);
      document.getElementById("bDownCount").textContent = fmtInt(b.down || 0);
      document.getElementById("bLdCount").textContent = fmtInt(b.limitDown || 0);
    }

    document.getElementById("heroAlertTotal").textContent = fmtInt(STATE.totalAlertCount);
  }

  function renderIndexCard(key, prefix) {
    const idx = STATE.indices[key];
    if (!idx || !idx.price) return;
    const priceEl = document.getElementById(`${prefix}Price`);
    const chgEl = document.getElementById(`${prefix}Chg`);
    const timeEl = document.getElementById(`${prefix}Time`);
    const rangeEl = document.getElementById(`${prefix}Range`);

    const cls = priceColorClass(idx.change);
    const arrow = idx.change > 0 ? "▲" : idx.change < 0 ? "▼" : "";
    priceEl.className = `pulse-price mono ${cls}`;
    priceEl.textContent = fmtNum(idx.price, 2);
    chgEl.className = `pulse-chg mono ${cls}`;
    chgEl.textContent = `${arrow} ${fmtNum(Math.abs(idx.change || 0), 2)} (${fmtSigned(idx.changePct, 2)}%)`;
    timeEl.textContent = idx.updatedAt || "--:--:--";
    rangeEl.textContent = `最高 ${fmtNum(idx.high, 2)} ｜ 最低 ${fmtNum(idx.low, 2)}`;
  }

  // ==================== 個股即時報價與戰情室 ====================
  async function selectStock(codeOrQuery, scrollToCockpit = false) {
    const cleanQuery = normalizeSearch(codeOrQuery);
    if (!cleanQuery) return false;
    STATE.hoverIndex = null;

    // 若本地已載入快取，先嘗試由代號或股名瞬間解析，提供零延遲反應
    let resolvedCode = cleanQuery;
    if (!STATE.stockMap.has(resolvedCode) && STATE.stocks.length > 0) {
      const exactName = STATE.stocks.find((s) => normalizeSearch(s.name) === cleanQuery);
      if (exactName) {
        resolvedCode = exactName.code;
      } else {
        const partial = STATE.stocks
          .filter((s) => normalizeSearch(s.code).includes(cleanQuery) || normalizeSearch(s.name).includes(cleanQuery))
          .sort((a, b) => (b.turnoverM || 0) - (a.turnoverM || 0));
        if (partial.length > 0) resolvedCode = partial[0].code;
      }
    }

    const snap = STATE.stockMap.get(resolvedCode);
    if (snap) {
      STATE.selectedCode = resolvedCode;
      renderStockHeroBasic(snap);
      renderLifelineStrip(snap);
    }

    if (scrollToCockpit) {
      const cockpitEl = document.getElementById("stockCockpitCard");
      if (cockpitEl && window.innerWidth <= 900) {
        cockpitEl.scrollIntoView({ behavior: "smooth", block: "start" });
      } else {
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    }

    const overlay = document.getElementById("chartLoadingOverlay");
    overlay.classList.remove("hidden");

    try {
      const res = await fetch(`/api/stock/${encodeURIComponent(resolvedCode)}?interval=${STATE.klineInterval}`);
      if (!res.ok) {
        pushToastAlert({
          category: "RISK",
          badge: "查詢提示",
          code: cleanQuery,
          name: "查無此台股代號或名稱",
          price: 0,
          changePct: 0,
          reason: `找不到符合「${codeOrQuery}」的上市、上櫃或興櫃個股，請確認代號或中文股名。`,
        });
        return false;
      }
      const detail = await res.json();
      const actualCode = detail.code || resolvedCode;
      STATE.selectedCode = actualCode;
      STATE.selectedDetail = detail;

      // 高亮快捷 chip
      document.querySelectorAll("#quickChips .chip").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.code === actualCode);
      });

      // 同步自訂到價表單預設代號
      const caCodeEl = document.getElementById("caCodeInput");
      if (caCodeEl) {
        caCodeEl.value = actualCode;
        updateCustomFormHint();
      }

      // 同步回全市場快取
      const existing = STATE.stockMap.get(actualCode);
      const tLatest = detail.tech || (detail.kline && detail.kline.latest) || {};
      if (existing) {
        Object.assign(existing, {
          price: detail.price,
          change: detail.change,
          changePct: detail.changePct,
          volumeLots: detail.volumeLots,
          quantScore: detail.quantScore,
          bidRatio: detail.bidRatio,
          weightedBidRatio: detail.weightedBidRatio,
          lifeline: tLatest.lifeline ?? existing.lifeline,
          lifelineBias: tLatest.lifelineBias ?? existing.lifelineBias,
          lifelineSignal: tLatest.lifelineSignal ?? existing.lifelineSignal,
          pocketPivotRatio: tLatest.pocketPivotRatio ?? existing.pocketPivotRatio,
          updatedAt: detail.updatedAt,
          alertBadges: (detail.alerts || []).slice(0, 3).map((a) => a.badge),
          alertCount: (detail.alerts || []).length,
        });
      }

      renderStockCockpitFull(detail);
      renderScreenerTable();
      return true;
    } catch (err) {
      console.error("Failed loading stock detail:", err);
      return false;
    } finally {
      overlay.classList.add("hidden");
    }
  }

  function renderStockHeroBasic(st, flashDir = 0) {
    const mktTag = document.getElementById("stMarketTag");
    mktTag.textContent = st.marketLabel || st.market;
    mktTag.className = "market-tag " + (st.market === "TPEX" ? "tpex" : st.market === "ESB" ? "esb" : "");

    document.getElementById("stCode").textContent = st.code;
    document.getElementById("stName").textContent = st.name;
    document.getElementById("stUpdated").textContent = `最後撮合更新：${st.updatedAt || "--:--:--"}`;

    const watchBtn = document.getElementById("watchStarBtn");
    const isWatched = STATE.watchlist.has(st.code);
    watchBtn.classList.toggle("watched", isWatched);
    watchBtn.textContent = isWatched ? "★ 已自選" : "☆ 自選";

    const cls = priceColorClass(st.change);
    const arrow = st.change > 0 ? "▲ " : st.change < 0 ? "▼ " : "";

    const priceEl = document.getElementById("stPrice");
    priceEl.className = `now-price mono ${cls}`;
    priceEl.textContent = fmtPrice(st.price);

    const chgEl = document.getElementById("stChange");
    chgEl.className = cls;
    chgEl.textContent = st.change !== null && st.change !== undefined ? `${arrow}${fmtPrice(Math.abs(st.change))}` : "--";

    const pctEl = document.getElementById("stChangePct");
    pctEl.className = cls;
    pctEl.textContent = `${fmtSigned(st.changePct, 2)}%`;

    if (flashDir !== 0) {
      const box = document.getElementById("stPriceBox");
      box.classList.remove("flash-up", "flash-down");
      void box.offsetWidth;
      box.classList.add(flashDir > 0 ? "flash-up" : "flash-down");
    }

    document.getElementById("qmOpen").textContent = fmtPrice(st.open);
    document.getElementById("qmHigh").textContent = fmtPrice(st.high);
    document.getElementById("qmLow").textContent = fmtPrice(st.low);
    document.getElementById("qmPrev").textContent = fmtPrice(st.prevClose);
    document.getElementById("qmLimitUp").textContent = st.limitUp ? fmtPrice(st.limitUp) : "無限制";
    document.getElementById("qmLimitDown").textContent = st.limitDown ? fmtPrice(st.limitDown) : "無限制";
    document.getElementById("qmVol").textContent = `${fmtInt(st.volumeLots)} 張`;
    document.getElementById("qmTurnover").textContent = `${fmtNum(st.turnoverM, 1)} 百萬`;
    document.getElementById("qmPe").textContent = st.pe ? fmtNum(st.pe, 1) : "--";
    document.getElementById("qmYield").textContent = st.dividendYield ? `${fmtNum(st.dividendYield, 2)}%` : "--";
    document.getElementById("qmPb").textContent = st.pb ? fmtNum(st.pb, 2) : "--";
    document.getElementById("qmQuantScore").textContent = `${st.quantScore ?? 50} / 100`;
  }

  function renderLifelineStrip(stOrDetail) {
    const t = (stOrDetail && (stOrDetail.tech || (stOrDetail.kline && stOrDetail.kline.latest))) || {};
    const lifeline = t.lifeline ?? stOrDetail?.lifeline ?? null;
    const price = stOrDetail?.price ?? null;

    const llPriceEl = document.getElementById("llPriceVal");
    const llPillEl = document.getElementById("llStatusPill");
    const llBiasEl = document.getElementById("llBiasVal");
    const llSpreadEl = document.getElementById("llSpreadVal");
    const llTdEl = document.getElementById("llTdVal");
    const llLockEl = document.getElementById("llLockVal");
    if (!llPriceEl) return;

    if (lifeline == null) {
      llPriceEl.textContent = "載入中...";
      llPillEl.className = "ll-status-pill";
      llPillEl.textContent = "計算量價成本中";
      llBiasEl.textContent = "--";
      llSpreadEl.textContent = "--";
      llTdEl.textContent = "--";
      llLockEl.textContent = "--";
      return;
    }

    llPriceEl.textContent = `${fmtPrice(lifeline)} 元`;
    const liveBias = price != null && lifeline > 0 ? ((price - lifeline) / lifeline) * 100 : (t.lifelineBias ?? null);
    if (liveBias != null) {
      llBiasEl.className = priceColorClass(liveBias);
      llBiasEl.textContent = `${fmtSigned(liveBias, 2)}%`;
    } else {
      llBiasEl.className = "";
      llBiasEl.textContent = "--";
    }

    const sig = t.lifelineSignal || (liveBias != null && liveBias >= 0 ? "ABOVE_BULL" : "BELOW_BEAR");
    let pillCls = "ll-status-pill";
    let pillTxt = "量價均衡";
    if (sig === "FLIP_BULL") {
      pillCls = "ll-status-pill st-bull";
      pillTxt = "★ 帶量躍過生命線翻多";
    } else if (sig === "PULLBACK_HOLD") {
      pillCls = "ll-status-pill st-gold";
      pillTxt = "★ 上揚生命線回踩守穩買點";
    } else if (sig === "BREAKDOWN") {
      pillCls = "ll-status-pill st-bear";
      pillTxt = "▼ 帶量失守生命線警戒";
    } else if (price != null ? price >= lifeline : sig === "ABOVE_BULL") {
      pillCls = "ll-status-pill st-bull";
      pillTxt = "▲ 站穩生命線・多頭控盤";
    } else {
      pillCls = "ll-status-pill st-bear";
      pillTxt = "▼ 位於生命線下・反壓整理";
    }
    llPillEl.className = pillCls;
    llPillEl.textContent = pillTxt;

    if (t.ribbonSpreadPrev != null) {
      llSpreadEl.className = t.ribbonSpreadPrev <= 2.6 ? "up-text" : "";
      llSpreadEl.textContent = `${fmtNum(t.ribbonSpreadPrev, 2)}%${t.ribbonSpreadPrev <= 2.6 ? " (麻花壓縮)" : ""}`;
    } else {
      llSpreadEl.className = "";
      llSpreadEl.textContent = "--";
    }

    const td = t.tdCount;
    if (td != null && td !== 0) {
      llTdEl.className = td > 0 ? "up-text" : "down-text";
      llTdEl.textContent = td > 0 ? `多方第 +${td} 轉` : `空方第 ${td} 轉`;
    } else {
      llTdEl.className = "";
      llTdEl.textContent = "區間醞釀";
    }

    const pp = t.pocketPivotRatio ?? stOrDetail?.pocketPivotRatio ?? null;
    if (pp != null) {
      llLockEl.className = pp >= 1.3 ? "up-text" : pp >= 1.0 ? "" : "down-text";
      llLockEl.textContent = `${fmtNum(pp, 2)}x${pp >= 1.3 ? " ★支點" : ""}`;
    } else {
      llLockEl.className = "";
      llLockEl.textContent = "--";
    }
  }

  function renderStockCockpitFull(detail) {
    renderStockHeroBasic(detail);
    renderLifelineStrip(detail);

    // 1. 個股觸發條件橫幅
    const banner = document.getElementById("stTriggeredBanner");
    const alerts = detail.alerts || [];
    if (alerts.length > 0) {
      banner.classList.remove("hidden");
      banner.innerHTML = alerts
        .map(
          (a) => `
          <div class="stb-item">
            <span class="stb-badge">🚨 ${escapeHtml(a.badge)}</span>
            <strong>${escapeHtml(a.title)}：</strong>
            <span>${escapeHtml(a.reason)}</span>
          </div>`
        )
        .join("");
    } else {
      banner.classList.add("hidden");
      banner.innerHTML = "";
    }

    // 2. 最佳五檔委買委賣与近端巨鯨壓差
    renderOrderbook(detail);

    // 3. 三大法人與自研戰法紀律診斷
    renderFactorDiagnosis(detail);

    // 4. K線與自研生命線戰法圖表
    renderKlineCanvas();
  }

  function renderOrderbook(detail) {
    const ob = detail.orderbook;
    const rowsEl = document.getElementById("orderbookRows");
    const ratioLabel = document.getElementById("obRatioLabel");
    const bidBar = document.getElementById("obBidBar");
    const askBar = document.getElementById("obAskBar");

    if (!ob || ((!ob.bids || ob.bids.length === 0) && (!ob.asks || ob.asks.length === 0))) {
      ratioLabel.textContent = detail.market === "ESB" ? "興櫃議價制" : "等待盤口五檔";
      bidBar.style.width = "50%";
      askBar.style.width = "50%";
      const hint =
        detail.bidPrice || detail.askPrice
          ? `最新委買 ${fmtPrice(detail.bidPrice)} 元 ／ 最新委賣 ${fmtPrice(detail.askPrice)} 元`
          : "目前暫無五檔掛單明細";
      rowsEl.innerHTML = `<div class="ob-empty">${hint}</div>`;
      return;
    }

    const bids = ob.bids || [];
    const asks = ob.asks || [];
    const bidRatio = detail.bidRatio ?? 50;
    const wRatio = detail.weightedBidRatio ?? bidRatio;
    const wAskRatio = Math.max(0, 100 - wRatio);
    ratioLabel.textContent = `近端買壓 ${fmtNum(wRatio, 1)}%（總委買 ${fmtNum(bidRatio, 0)}%）`;
    bidBar.style.width = `${wRatio}%`;
    askBar.style.width = `${wAskRatio}%`;

    const maxLots = Math.max(
      1,
      ...bids.map((x) => x.lots || 0),
      ...asks.map((x) => x.lots || 0)
    );

    let html = "";
    for (let i = 0; i < 5; i++) {
      const b = bids[i];
      const a = asks[i];
      const bPct = b ? Math.min(100, Math.round(((b.lots || 0) / maxLots) * 100)) : 0;
      const aPct = a ? Math.min(100, Math.round(((a.lots || 0) / maxLots) * 100)) : 0;
      html += `
        <div class="ob-row" style="background: linear-gradient(90deg, rgba(255,77,94,0.14) ${bPct / 2}%, transparent ${bPct / 2}%, transparent ${100 - aPct / 2}%, rgba(16,185,129,0.14) ${100 - aPct / 2}%);">
          <span class="ob-bid-vol">${b ? fmtInt(b.lots) : "--"}</span>
          <span class="ob-bid-px">${b ? fmtPrice(b.price) : "--"}</span>
          <span class="ob-ask-px">${a ? fmtPrice(a.price) : "--"}</span>
          <span class="ob-ask-vol">${a ? fmtInt(a.lots) : "--"}</span>
        </div>
      `;
    }
    rowsEl.innerHTML = html;
  }

  function renderFactorDiagnosis(detail) {
    const fmtInst = (elId, val) => {
      const el = document.getElementById(elId);
      const n = Number(val || 0);
      el.className = priceColorClass(n);
      el.textContent = `${n > 0 ? "+" : ""}${fmtInt(n)} 張`;
    };
    fmtInst("instForeign", detail.foreignNetLots);
    fmtInst("instTrust", detail.trustNetLots);
    fmtInst("instDealer", detail.dealerNetLots);
    fmtInst("instTotal", detail.instTotalLots);

    const t = detail.tech || (detail.kline && detail.kline.latest) || {};
    document.getElementById("diagRsi").textContent = t.rsi14 != null ? fmtNum(t.rsi14, 1) : "--";
    document.getElementById("diagKd").textContent =
      t.k9 != null && t.d9 != null ? `K ${fmtNum(t.k9, 1)} / D ${fmtNum(t.d9, 1)}` : "--";
    document.getElementById("diagMacd").textContent = t.macdHist != null ? fmtSigned(t.macdHist, 2) : "--";
    document.getElementById("diagVolRatio").textContent = t.volRatio20 != null ? `${fmtNum(t.volRatio20, 2)}x` : "--";

    const wickEl = document.getElementById("diagMa20Bias");
    if (t.lowerWickPct != null) {
      wickEl.className = t.lowerWickPct >= 45 ? "up-text" : "";
      wickEl.textContent = `${fmtNum(t.lowerWickPct, 1)}%${t.lowerWickPct >= 45 ? " ★神針" : ""}`;
    } else {
      wickEl.className = "";
      wickEl.textContent = "--";
    }
    document.getElementById("diagBbPct").textContent = t.bbPctB != null ? fmtNum(t.bbPctB, 2) : "--";

    // 自研量化綜合判讀結論
    const score = detail.quantScore ?? 50;
    const verdictEl = document.getElementById("quantVerdictBadge");
    let verdict = "區間中性觀望";
    let vColor = "rgba(148,163,184,0.2)";
    let tColor = "#cbd5e1";

    if (score >= 72) {
      verdict = "🔥 生命線多頭強振";
      vColor = "rgba(255,77,94,0.22)";
      tColor = "#fda4af";
    } else if (score >= 58) {
      verdict = "▲ 線上偏多佈局";
      vColor = "rgba(245,158,11,0.2)";
      tColor = "#fde68a";
    } else if (score <= 35) {
      verdict = "❄️ 線下破位警戒";
      vColor = "rgba(16,185,129,0.22)";
      tColor = "#6ee7b7";
    }
    verdictEl.textContent = `${verdict} (${score}分)`;
    verdictEl.style.background = vColor;
    verdictEl.style.color = tColor;

    const notes = [];
    if (t.lifeline != null && detail.price != null) {
      const bias = ((detail.price - t.lifeline) / t.lifeline) * 100;
      if (t.lifelineSignal === "FLIP_BULL") {
        notes.push(`【生命線紀律】股價今日帶量躍過 α-生命線 (${fmtPrice(t.lifeline)} 元)，空翻多轉強，防守點設於 ${fmtPrice(t.lifeline)} 元`);
      } else if (t.lifelineSignal === "PULLBACK_HOLD") {
        notes.push(`【生命線紀律】α-生命線 (${fmtPrice(t.lifeline)} 元) 維持上揚且回踩不破，屬低風險黃金守線位階`);
      } else if (detail.price >= t.lifeline) {
        notes.push(`【生命線紀律】現價站穩自研 α-生命線 (${fmtPrice(t.lifeline)} 元，乖離 ${fmtSigned(bias, 2)}%) 之上，未跌破生命線前維持波段偏多思維`);
      } else {
        notes.push(`【生命線紀律】現價位於自研 α-生命線 (${fmtPrice(t.lifeline)} 元，負乖離 ${fmtSigned(bias, 2)}%) 之下，生命線轉為上檔壓力，宜待帶量站回再行進場`);
      }
    }
    if (t.ribbonBreakout) {
      notes.push("【乾坤三線 2.0】前期三日均線極度麻花壓縮後實體紅K旱地拔蔥貫穿，波段爆發力轉強");
    } else if (t.ribbonSpreadPrev != null && t.ribbonSpreadPrev <= 2.6) {
      notes.push(`【乾坤三線 2.0】短中長三線糾結度壓縮至 ${fmtNum(t.ribbonSpreadPrev, 2)}%，變盤在即留意帶量突破方向`);
    }
    if (t.pocketPivotRatio != null && t.pocketPivotRatio >= 1.3 && (detail.changePct || 0) > 0) {
      notes.push(`【主力口袋支點】今日攻擊量達前10日最大黑K出貨量之 ${fmtNum(t.pocketPivotRatio, 2)} 倍，吃貨量能明確主導`);
    }
    if (t.upperWickPct != null && t.upperWickPct >= 46) {
      notes.push(`【K棒結構濾網】上影線佔振幅 ${fmtNum(t.upperWickPct, 0)}%，留意上檔短線調節反壓，切忌追高假突破`);
    }
    if ((detail.trustNetLots || 0) >= 150 && (detail.foreignNetLots || 0) > 0) {
      notes.push("【籌碼共振】投信與外資同步買超，下檔具備法人護盤支撐");
    } else if ((detail.instTotalLots || 0) < -1000) {
      notes.push("三大法人短線調節賣壓偏重，留意籌碼鬆動");
    }
    if (t.tdCount != null && t.tdCount <= -7) {
      notes.push(`【九轉極值】空方連續衰竭已達第 ${Math.abs(t.tdCount)} 轉，切勿殺低並留意長下影神針止跌訊號`);
    } else if (t.tdCount != null && t.tdCount >= 8) {
      notes.push(`【九轉極值】多方急漲達第 +${t.tdCount} 轉高檔區，宜沿短攻 EMA5 / α-生命線執行移動停利`);
    }
    const wBid = detail.weightedBidRatio ?? detail.bidRatio;
    if (wBid != null && wBid >= 66) {
      notes.push(`【盤口巨鯨】近端加權委買高達 ${fmtNum(wBid, 1)}%，內外盤承接積極`);
    }

    document.getElementById("quantCommentary").textContent =
      notes.length > 0 ? notes.join("；") + "。" : "自研 α-生命線 2.0 與量價結構模型持續監控即時撮合變化中。";
  }

  // ==================== 高解析度 K 線與技術指標 Canvas 繪圖引擎 ====================
  function renderKlineCanvas() {
    const canvas = document.getElementById("klineCanvas");
    if (!canvas) return;
    const rect = canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const W = Math.max(320, Math.floor(rect.width));
    const H = Math.max(260, Math.floor(rect.height));
    canvas.width = W * dpr;
    canvas.height = H * dpr;

    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    const kline = STATE.selectedDetail && STATE.selectedDetail.kline;
    const allCandles = (kline && kline.candles) || [];
    if (allCandles.length === 0) {
      ctx.fillStyle = "#64748b";
      ctx.font = '13px "Noto Sans TC", sans-serif';
      ctx.textAlign = "center";
      ctx.fillText("暫無歷史 K 線資料（僅顯示即時報價）", W / 2, H / 2);
      return;
    }

    // 依使用者選擇縮放根數顯示 K 棒（預設 55 根，讓每根 K 棒與戰法標記清晰放大）
    const maxBars = Math.min(allCandles.length, STATE.klineBarCount || 55);
    const startOffset = allCandles.length - maxBars;
    const candles = allCandles.slice(startOffset);
    const series = (kline && kline.series) || {};

    const sliceSeries = (arr) => (Array.isArray(arr) ? arr.slice(startOffset) : []);
    const lifeline = sliceSeries(series.lifeline);
    const ema5 = sliceSeries(series.ema5 || series.ma5);
    const ema10 = sliceSeries(series.ema10 || series.ma20);
    const ma60 = sliceSeries(series.ma60);
    const bbUp = sliceSeries(series.bbUpper);
    const bbLo = sliceSeries(series.bbLower);
    const rsi14 = sliceSeries(series.rsi14);
    const k9 = sliceSeries(series.k9);
    const d9 = sliceSeries(series.d9);
    const dif = sliceSeries(series.macdDif);
    const dea = sliceSeries(series.macdDea);
    const hist = sliceSeries(series.macdHist);
    const visibleSignals = ((kline && kline.signals) || [])
      .filter((sg) => sg.idx >= startOffset && sg.idx < allCandles.length)
      .map((sg) => ({ ...sg, localIdx: sg.idx - startOffset }));

    const sigByLocalIdx = new Map();
    for (const sg of visibleSignals) {
      if (!sigByLocalIdx.has(sg.localIdx)) sigByLocalIdx.set(sg.localIdx, []);
      sigByLocalIdx.get(sg.localIdx).push(sg);
    }

    const isMobileCanvas = W < 500;
    const padL = 12;
    const padR = 66;
    const padT = 32;
    const padB = 22;
    const mainH = Math.floor((H - padT - padB) * 0.68);
    const gapH = 14;
    const subTop = padT + mainH + gapH;
    const subH = H - padB - subTop;
    const plotW = W - padL - padR;

    // 計算主圖價格上下界（預留上下 17% 空間供 K 棒直連引線與高對比買賣標記完整展開不裁切）
    let pMin = Infinity;
    let pMax = -Infinity;
    for (let i = 0; i < candles.length; i++) {
      const c = candles[i];
      if (c.low < pMin) pMin = c.low;
      if (c.high > pMax) pMax = c.high;
      [lifeline[i], ema5[i], ema10[i], ma60[i], bbUp[i], bbLo[i]].forEach((v) => {
        if (v != null) {
          if (v < pMin) pMin = v;
          if (v > pMax) pMax = v;
        }
      });
    }
    const pSpan = Math.max(0.01, pMax - pMin);
    pMin -= pSpan * 0.17;
    pMax += pSpan * 0.17;

    const xOf = (i) => padL + ((i + 0.5) / candles.length) * plotW;
    const yOfPrice = (p) => padT + ((pMax - p) / (pMax - pMin)) * mainH;

    // 繪製背景格線與右側價格刻度
    ctx.strokeStyle = "rgba(148, 163, 184, 0.1)";
    ctx.lineWidth = 1;
    ctx.font = '11px "JetBrains Mono", monospace';
    ctx.fillStyle = "#94a3b8";
    ctx.textAlign = "left";

    for (let g = 0; g <= 4; g++) {
      const y = padT + (mainH / 4) * g;
      const pv = pMax - ((pMax - pMin) / 4) * g;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.fillText(fmtPrice(pv), padL + plotW + 6, y + 4);
    }

    // 布林通道背景帶
    ctx.save();
    ctx.beginPath();
    let started = false;
    const validBbIdx = [];
    for (let i = 0; i < candles.length; i++) {
      if (bbUp[i] != null && bbLo[i] != null) {
        validBbIdx.push(i);
        const x = xOf(i);
        const y = yOfPrice(bbUp[i]);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
    }
    for (let j = validBbIdx.length - 1; j >= 0; j--) {
      const i = validBbIdx[j];
      ctx.lineTo(xOf(i), yOfPrice(bbLo[i]));
    }
    if (validBbIdx.length > 1) {
      ctx.closePath();
      ctx.fillStyle = "rgba(56, 189, 248, 0.045)";
      ctx.fill();
    }
    ctx.restore();

    const barW = Math.max(3, Math.min(14, (plotW / candles.length) * 0.72));

    // 對觸發自研戰法訊號的關鍵 K 棒繪製垂直高亮光帶與副圖對齊虛線，一眼鎖定對應價量
    ctx.save();
    for (const [sIdx, sList] of sigByLocalIdx.entries()) {
      const sx = xOf(sIdx);
      const hasBuy = sList.some((s) => s.type === "BUY");
      const bandW = Math.max(barW + 6, 12);
      ctx.fillStyle = hasBuy ? "rgba(251, 191, 36, 0.075)" : "rgba(16, 185, 129, 0.075)";
      ctx.fillRect(sx - bandW / 2, padT, bandW, mainH);

      ctx.strokeStyle = hasBuy ? "rgba(251, 191, 36, 0.22)" : "rgba(16, 185, 129, 0.22)";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(sx, subTop);
      ctx.lineTo(sx, subTop + subH);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.restore();

    // 繪製 K 線蠟燭（台股慣例：紅漲 close >= open，綠跌 close < open）
    for (let i = 0; i < candles.length; i++) {
      const c = candles[i];
      const isUp = c.close >= c.open;
      const color = isUp ? "#ff4d5e" : "#10b981";
      const x = xOf(i);
      const yHigh = yOfPrice(c.high);
      const yLow = yOfPrice(c.low);
      const yOpen = yOfPrice(c.open);
      const yClose = yOfPrice(c.close);

      ctx.strokeStyle = color;
      ctx.lineWidth = 1.35;
      ctx.beginPath();
      ctx.moveTo(x, yHigh);
      ctx.lineTo(x, yLow);
      ctx.stroke();

      const topY = Math.min(yOpen, yClose);
      const bodyH = Math.max(1.8, Math.abs(yClose - yOpen));
      ctx.fillStyle = color;
      ctx.fillRect(x - barW / 2, topY, barW, bodyH);

      // 若該根 K 棒觸發自研訊號，於實體外框加上高亮描邊強化辨識
      if (sigByLocalIdx.has(i)) {
        const sList = sigByLocalIdx.get(i);
        const isBuyBar = sList.some((s) => s.type === "BUY");
        ctx.save();
        ctx.strokeStyle = isBuyBar ? "#fde047" : "#6ee7b7";
        ctx.lineWidth = 1.25;
        ctx.strokeRect(x - barW / 2 - 0.5, topY - 0.5, barW + 1, bodyH + 1);
        ctx.restore();
      }
    }

    // 繪製均線
    const drawLine = (arr, color, width = 1.4, dash = []) => {
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.setLineDash(dash);
      ctx.beginPath();
      let st = false;
      for (let i = 0; i < arr.length; i++) {
        if (arr[i] == null) continue;
        const x = xOf(i);
        const y = yOfPrice(arr[i]);
        if (!st) {
          ctx.moveTo(x, y);
          st = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
      ctx.restore();
    };

    drawLine(bbUp, "rgba(100,116,139,0.40)", 1, [3, 3]);
    drawLine(bbLo, "rgba(100,116,139,0.40)", 1, [3, 3]);
    drawLine(ema5, "#fbbf24", 1.35);
    drawLine(ema10, "#38bdf8", 1.35);
    drawLine(ma60, "#c084fc", 1.3);

    // ★ 繪製自研【α-量價多空生命線】：上揚呈琥珀亮金 (#ff9f1c)，下彎呈青色警戒 (#06b6d4)
    ctx.save();
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.lineWidth = 2.85;
    for (let i = 1; i < lifeline.length; i++) {
      if (lifeline[i - 1] == null || lifeline[i] == null) continue;
      const rising = lifeline[i] >= lifeline[i - 1];
      ctx.strokeStyle = rising ? "#ff9f1c" : "#06b6d4";
      ctx.shadowColor = rising ? "rgba(255, 159, 28, 0.5)" : "rgba(6, 182, 212, 0.38)";
      ctx.shadowBlur = 6;
      ctx.beginPath();
      ctx.moveTo(xOf(i - 1), yOfPrice(lifeline[i - 1]));
      ctx.lineTo(xOf(i), yOfPrice(lifeline[i]));
      ctx.stroke();
    }
    ctx.restore();

    // 右軸標示最新生命線防守價標籤
    const lastLl = lifeline[lifeline.length - 1];
    if (lastLl != null && lastLl >= pMin && lastLl <= pMax) {
      const ly = yOfPrice(lastLl);
      ctx.save();
      ctx.fillStyle = "rgba(255, 159, 28, 0.95)";
      ctx.fillRect(padL + plotW + 2, ly - 8.5, padR - 4, 17);
      ctx.fillStyle = "#060913";
      ctx.font = '700 10.5px "JetBrains Mono", monospace';
      ctx.textAlign = "left";
      ctx.fillText(fmtPrice(lastLl), padL + plotW + 5, ly + 3.5);
      ctx.restore();
    }

    // ★ 繪製超清晰三段式 K 棒戰法訊號標記（發光錨點圓環 ＋ 方向箭頭直連引線 ＋ 分層防重疊高對比膠囊）
    const getSignalPalette = (sg) => {
      switch (sg.code) {
        case "SQUEEZE":
          return { bg: "#e11d48", border: "#fda4af", text: "#ffffff", stem: "#fb7185", halo: "rgba(251,113,133,0.38)" };
        case "FLIP":
          return { bg: "#ef4444", border: "#fecaca", text: "#ffffff", stem: "#f87171", halo: "rgba(248,113,113,0.38)" };
        case "HOLD":
          return { bg: "#f59e0b", border: "#fef08a", text: "#060913", stem: "#fbbf24", halo: "rgba(251,191,36,0.42)" };
        case "POCKET":
          return { bg: "#9333ea", border: "#e9d5ff", text: "#ffffff", stem: "#c084fc", halo: "rgba(192,132,252,0.40)" };
        case "TD_BOT":
          return { bg: "#0284c7", border: "#bae6fd", text: "#ffffff", stem: "#38bdf8", halo: "rgba(56,189,248,0.38)" };
        case "BREAK":
          return { bg: "#059669", border: "#6ee7b7", text: "#ffffff", stem: "#10b981", halo: "rgba(16,185,129,0.38)" };
        case "TD_TOP":
          return { bg: "#0d9488", border: "#99f6e4", text: "#ffffff", stem: "#2dd4bf", halo: "rgba(45,212,191,0.38)" };
        default:
          return sg.type === "BUY"
            ? { bg: "#e11d48", border: "#fda4af", text: "#ffffff", stem: "#fb7185", halo: "rgba(251,113,133,0.38)" }
            : { bg: "#059669", border: "#6ee7b7", text: "#ffffff", stem: "#10b981", halo: "rgba(16,185,129,0.38)" };
      }
    };

    const placedBuyTiers = [[], [], []];
    const placedSellTiers = [[], [], []];
    const badgeFont = isMobileCanvas
      ? '800 11px "Noto Sans TC", sans-serif'
      : '800 12px "Noto Sans TC", sans-serif';
    const badgeH = isMobileCanvas ? 19 : 21;

    for (const sg of visibleSignals) {
      const i = sg.localIdx;
      const c = candles[i];
      if (!c) continue;
      const x = xOf(i);
      const isBuy = sg.type === "BUY";
      const pal = getSignalPalette(sg);

      ctx.save();
      ctx.font = badgeFont;
      const labelText = sg.tag || (isBuy ? "買★訊號" : "賣▼訊號");
      const tw = Math.ceil(ctx.measureText(labelText).width) + (isMobileCanvas ? 10 : 14);
      const bx = Math.min(padL + plotW - tw - 1, Math.max(padL + 1, x - tw / 2));

      // 自動尋找不與同側鄰近標籤碰撞的垂直階層 (Tier 0 / 1 / 2)
      const sideTiers = isBuy ? placedBuyTiers : placedSellTiers;
      let chosenTier = -1;
      for (let t = 0; t < sideTiers.length; t++) {
        const collides = sideTiers[t].some(([l, r]) => !(bx + tw + 4 < l || bx - 4 > r));
        if (!collides) {
          chosenTier = t;
          break;
        }
      }
      if (chosenTier === -1) {
        if (i < candles.length - 3) {
          ctx.restore();
          continue;
        }
        chosenTier = sideTiers.length - 1;
      }
      sideTiers[chosenTier].push([bx, bx + tw]);

      const wickY = isBuy ? yOfPrice(c.low) : yOfPrice(c.high);
      const stemBase = isMobileCanvas ? 14 : 16;
      const tierStep = isMobileCanvas ? 22 : 25;
      const stemDist = stemBase + chosenTier * tierStep;

      const by = isBuy
        ? Math.min(padT + mainH - badgeH - 2, wickY + stemDist)
        : Math.max(padT + 2, wickY - stemDist - badgeH);

      // (1) K 棒影線頂點發光鎖定圓環
      ctx.fillStyle = pal.halo;
      ctx.beginPath();
      ctx.arc(x, wickY, 5.5, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = pal.stem;
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      ctx.arc(x, wickY, 2.7, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();

      // (2) 垂直指向引線與銳利方向箭頭 (▲ 指向買進 K 棒下影線 / ▼ 指向賣出 K 棒上影線)
      ctx.strokeStyle = pal.stem;
      ctx.fillStyle = pal.stem;
      ctx.lineWidth = 1.85;
      if (isBuy) {
        const tipY = wickY + 3.5;
        const arrowBaseY = Math.min(by, tipY + 6);
        ctx.beginPath();
        ctx.moveTo(x, by);
        ctx.lineTo(x, arrowBaseY);
        ctx.stroke();

        ctx.beginPath();
        ctx.moveTo(x, tipY);
        ctx.lineTo(x - 4, arrowBaseY);
        ctx.lineTo(x + 4, arrowBaseY);
        ctx.closePath();
        ctx.fill();
      } else {
        const tipY = wickY - 3.5;
        const arrowBaseY = Math.max(by + badgeH, tipY - 6);
        ctx.beginPath();
        ctx.moveTo(x, by + badgeH);
        ctx.lineTo(x, arrowBaseY);
        ctx.stroke();

        ctx.beginPath();
        ctx.moveTo(x, tipY);
        ctx.lineTo(x - 4, arrowBaseY);
        ctx.lineTo(x + 4, arrowBaseY);
        ctx.closePath();
        ctx.fill();
      }

      // (3) 高對比立體邊框戰法標籤膠囊
      ctx.shadowColor = "rgba(0, 0, 0, 0.85)";
      ctx.shadowBlur = 7;
      ctx.shadowOffsetY = 2;
      ctx.fillStyle = pal.bg;
      ctx.strokeStyle = pal.border;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      if (ctx.roundRect) {
        ctx.roundRect(bx, by, tw, badgeH, 5);
      } else {
        ctx.rect(bx, by, tw, badgeH);
      }
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.shadowOffsetY = 0;
      ctx.stroke();

      ctx.fillStyle = pal.text;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(labelText, bx + tw / 2, by + badgeH / 2 + 0.5);
      ctx.restore();
    }

    // 最新現價水平虛線
    const curPrice = STATE.selectedDetail.price;
    if (curPrice && curPrice >= pMin && curPrice <= pMax) {
      const cy = yOfPrice(curPrice);
      ctx.save();
      ctx.strokeStyle = "rgba(255, 255, 255, 0.45)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(padL, cy);
      ctx.lineTo(padL + plotW, cy);
      ctx.stroke();
      ctx.restore();
    }

    // ==================== 副圖繪製 (VOL / KD / RSI / MACD) ====================
    ctx.strokeStyle = "rgba(148, 163, 184, 0.14)";
    ctx.beginPath();
    ctx.moveTo(padL, subTop);
    ctx.lineTo(padL + plotW, subTop);
    ctx.stroke();

    const subType = STATE.subChartType;
    if (subType === "VOL") {
      const maxV = Math.max(1, ...candles.map((c) => c.volumeLots || 0));
      for (let i = 0; i < candles.length; i++) {
        const c = candles[i];
        const isUp = c.close >= c.open;
        ctx.fillStyle = isUp ? "rgba(255, 77, 94, 0.65)" : "rgba(16, 185, 129, 0.65)";
        const h = ((c.volumeLots || 0) / maxV) * (subH - 4);
        ctx.fillRect(xOf(i) - barW / 2, subTop + subH - h, barW, h);
      }
      ctx.fillStyle = "#94a3b8";
      ctx.fillText(`${fmtInt(maxV)}張`, padL + plotW + 4, subTop + 11);
    } else if (subType === "KD") {
      const ySub = (v) => subTop + ((100 - v) / 100) * subH;
      // 20 / 80 警戒線
      [80, 20].forEach((lv) => {
        ctx.save();
        ctx.strokeStyle = "rgba(148,163,184,0.2)";
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(padL, ySub(lv));
        ctx.lineTo(padL + plotW, ySub(lv));
        ctx.stroke();
        ctx.restore();
        ctx.fillStyle = "#64748b";
        ctx.fillText(String(lv), padL + plotW + 6, ySub(lv) + 4);
      });
      const drawSub = (arr, color) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        let s = false;
        arr.forEach((v, i) => {
          if (v == null) return;
          const x = xOf(i), y = ySub(v);
          if (!s) { ctx.moveTo(x, y); s = true; } else ctx.lineTo(x, y);
        });
        ctx.stroke();
      };
      drawSub(k9, "#fbbf24");
      drawSub(d9, "#38bdf8");
    } else if (subType === "RSI") {
      const ySub = (v) => subTop + ((100 - v) / 100) * subH;
      [70, 30].forEach((lv) => {
        ctx.save();
        ctx.strokeStyle = "rgba(148,163,184,0.22)";
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(padL, ySub(lv));
        ctx.lineTo(padL + plotW, ySub(lv));
        ctx.stroke();
        ctx.restore();
        ctx.fillStyle = "#64748b";
        ctx.fillText(String(lv), padL + plotW + 6, ySub(lv) + 4);
      });
      ctx.strokeStyle = "#f472b6";
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      let s = false;
      rsi14.forEach((v, i) => {
        if (v == null) return;
        const x = xOf(i), y = ySub(v);
        if (!s) { ctx.moveTo(x, y); s = true; } else ctx.lineTo(x, y);
      });
      ctx.stroke();
    } else if (subType === "MACD") {
      const vals = [...dif, ...dea, ...hist].filter((v) => v != null);
      const maxAbs = Math.max(0.1, ...vals.map((v) => Math.abs(v)));
      const ySub = (v) => subTop + subH / 2 - (v / maxAbs) * (subH * 0.44);
      const zeroY = ySub(0);
      ctx.strokeStyle = "rgba(148,163,184,0.25)";
      ctx.beginPath();
      ctx.moveTo(padL, zeroY);
      ctx.lineTo(padL + plotW, zeroY);
      ctx.stroke();

      hist.forEach((v, i) => {
        if (v == null) return;
        ctx.fillStyle = v >= 0 ? "rgba(255,77,94,0.75)" : "rgba(16,185,129,0.75)";
        const y = ySub(v);
        ctx.fillRect(xOf(i) - barW / 2, Math.min(zeroY, y), barW, Math.max(1, Math.abs(y - zeroY)));
      });
      const drawSub = (arr, color) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.3;
        ctx.beginPath();
        let s = false;
        arr.forEach((v, i) => {
          if (v == null) return;
          const x = xOf(i), y = ySub(v);
          if (!s) { ctx.moveTo(x, y); s = true; } else ctx.lineTo(x, y);
        });
        ctx.stroke();
      };
      drawSub(dif, "#fbbf24");
      drawSub(dea, "#38bdf8");
    }

    // X 軸日期標籤
    ctx.fillStyle = "#64748b";
    ctx.textAlign = "center";
    const stepIdx = Math.max(1, Math.floor(candles.length / 5));
    for (let i = 0; i < candles.length; i += stepIdx) {
      ctx.fillText(candles[i].time, xOf(i), H - 5);
    }

    // 滑鼠十字準線、頂部 K 棒戰法訊號詳解橫幅與指標讀數更新
    const idx = STATE.hoverIndex != null && STATE.hoverIndex < candles.length ? STATE.hoverIndex : candles.length - 1;
    const hc = candles[idx];
    if (STATE.hoverIndex != null && STATE.hoverIndex < candles.length) {
      const hx = xOf(idx);
      ctx.save();
      ctx.strokeStyle = "rgba(255,255,255,0.42)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(hx, padT);
      ctx.lineTo(hx, H - padB);
      ctx.stroke();
      ctx.restore();
    }

    // 圖表左上角即時顯示「滑鼠指向之 K 棒訊號」或「最近一次觸發之 K 棒戰法詳解」
    const hoveredSigs = STATE.hoverIndex != null ? sigByLocalIdx.get(idx) : null;
    const activeSig =
      hoveredSigs && hoveredSigs.length > 0
        ? hoveredSigs[hoveredSigs.length - 1]
        : visibleSignals.length > 0
        ? visibleSignals[visibleSignals.length - 1]
        : null;

    if (activeSig && candles[activeSig.localIdx]) {
      const sc = candles[activeSig.localIdx];
      const pal = getSignalPalette(activeSig);
      const prefix = hoveredSigs && hoveredSigs.length > 0 ? "🎯 指向K棒" : "🎯 近期訊號";
      let bannerText = `${prefix} ${sc.time}【${activeSig.tag}】${activeSig.detail || `觸發價 ${fmtPrice(activeSig.price)}`}`;

      ctx.save();
      ctx.font = isMobileCanvas
        ? '700 10.5px "Noto Sans TC", sans-serif'
        : '700 11.5px "Noto Sans TC", sans-serif';
      const maxBannerW = plotW - 8;
      while (bannerText.length > 12 && ctx.measureText(bannerText).width + 20 > maxBannerW) {
        bannerText = bannerText.slice(0, -2) + "…";
      }
      const bw = Math.min(maxBannerW, Math.ceil(ctx.measureText(bannerText).width) + 18);
      const bh = 22;
      const bx = padL + 2;
      const by = 5;

      ctx.fillStyle = "rgba(9, 14, 28, 0.90)";
      ctx.strokeStyle = pal.border;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      if (ctx.roundRect) {
        ctx.roundRect(bx, by, bw, bh, 5);
      } else {
        ctx.rect(bx, by, bw, bh);
      }
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = "#f8fafc";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(bannerText, bx + 9, by + bh / 2 + 0.5);
      ctx.restore();
    }

    const legEl = document.getElementById("chartHoverLegend");
    if (legEl && hc) {
      const barSigs = sigByLocalIdx.get(idx) || [];
      const sigHtml = barSigs
        .map(
          (s) =>
            `<span style="color:${s.type === "BUY" ? "#fde047" : "#6ee7b7"};font-weight:800">【${escapeHtml(s.tag)}】</span>`
        )
        .join("");
      legEl.innerHTML = `
        <span style="color:#e2e8f0">${escapeHtml(hc.time)} 開${fmtPrice(hc.open)} 高${fmtPrice(hc.high)} 低${fmtPrice(hc.low)} 收${fmtPrice(hc.close)}</span>
        ${sigHtml}
        <span class="leg-lifeline">★ α-生命線:${lifeline[idx] != null ? fmtPrice(lifeline[idx]) : "--"}</span>
        <span class="leg-ma5">● 短攻EMA5:${ema5[idx] != null ? fmtPrice(ema5[idx]) : "--"}</span>
        <span class="leg-ma20">● 輔攻EMA10:${ema10[idx] != null ? fmtPrice(ema10[idx]) : "--"}</span>
        <span class="leg-ma60">● 季線MA60:${ma60[idx] != null ? fmtPrice(ma60[idx]) : "--"}</span>
      `;
    }

    // 儲存幾何供滑鼠換算索引
    canvas._chartMeta = { padL, plotW, count: candles.length };
  }

  // ==================== 即時警示雷達渲染 ====================
  function renderAlertRadar() {
    const cat = STATE.alertCategoryFilter;
    const filtered = cat === "ALL" ? STATE.alerts : STATE.alerts.filter((a) => a.category === cat);

    document.getElementById("radarCountPill").textContent = `${filtered.length} 筆`;
    document.getElementById("heroAlertTotal").textContent = fmtInt(STATE.totalAlertCount);

    const listEl = document.getElementById("alertFeedList");
    if (filtered.length === 0) {
      listEl.innerHTML = `<div class="ob-empty">目前此分類尚無觸發條件之個股；可調整下方條件門檻或開啟「⚡ 模擬盤中跳動」測試。</div>`;
      return;
    }

    listEl.innerHTML = filtered
      .slice(0, 120)
      .map((a) => {
        const cls = priceColorClass(a.changePct);
        return `
          <div class="alert-card cat-${escapeHtml(a.category)}" data-code="${escapeHtml(a.code)}">
            <div class="ac-top">
              <div class="ac-stock">
                <span class="mono" style="color:var(--accent-cyan)">${escapeHtml(a.code)}</span>
                <span>${escapeHtml(a.name)}</span>
                <span class="ac-badge">${escapeHtml(a.badge)}</span>
              </div>
              <div class="mono ${cls}" style="font-weight:700;">
                ${fmtPrice(a.price)} (${fmtSigned(a.changePct, 2)}%)
              </div>
            </div>
            <div class="ac-reason">${escapeHtml(a.reason)}</div>
            <div class="ac-foot mono">
              <span>${escapeHtml(a.marketLabel)} · 量化評分 ${a.quantScore ?? "--"}</span>
              <span>觸發時間 ${escapeHtml(a.triggeredAt || "--")}</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  // ==================== 量化條件設定與自訂到價警示 ====================
  function renderRulesConfig() {
    const container = document.getElementById("rulesListContainer");
    if (!container) return;

    container.innerHTML = STATE.rules
      .map((r) => {
        const hits = STATE.ruleHitCounts[r.id] ?? 0;
        const paramFields = Object.entries(r.params || {})
          .map(
            ([pk, p]) => `
            <label class="rule-param-field">
              <span>${escapeHtml(p.label)}</span>
              <input
                type="number"
                data-rule-id="${escapeHtml(r.id)}"
                data-param-key="${escapeHtml(pk)}"
                step="${p.step ?? 0.5}"
                min="${p.min ?? -100}"
                max="${p.max ?? 100000}"
                value="${p.value}"
              />
            </label>`
          )
          .join("");

        return `
          <div class="rule-item">
            <div class="rule-top">
              <label class="rule-label-wrap">
                <input type="checkbox" class="rule-enable-chk" data-rule-id="${escapeHtml(r.id)}" ${r.enabled ? "checked" : ""} />
                <span>${escapeHtml(r.badge)}｜${escapeHtml(r.name)}</span>
              </label>
              <span class="rule-hit-badge mono" id="hit_${escapeHtml(r.id)}">觸發 ${hits} 檔</span>
            </div>
            <p class="rule-desc">${escapeHtml(r.description)}</p>
            <div class="rule-params">${paramFields}</div>
          </div>
        `;
      })
      .join("");
  }

  function renderCustomAlertsList() {
    const tabCnt = document.getElementById("customAlertCountTab");
    if (tabCnt) tabCnt.textContent = String(STATE.customPriceAlerts.length);

    const listEl = document.getElementById("customAlertsList");
    if (!listEl) return;

    if (STATE.customPriceAlerts.length === 0) {
      listEl.innerHTML = `<div class="ob-empty" style="padding:14px;">尚未新增自訂個股到價警示，可於上方輸入目標價新增。</div>`;
      return;
    }

    listEl.innerHTML = STATE.customPriceAlerts
      .map((ca) => {
        const st = STATE.stockMap.get(ca.code);
        const nowP = st ? fmtPrice(st.price) : "--";
        const isHit =
          st && st.price != null
            ? ca.operator === ">="
              ? st.price >= ca.targetPrice
              : st.price <= ca.targetPrice
            : false;

        return `
          <div class="ca-row">
            <div>
              <strong class="mono" style="color:var(--accent-cyan);cursor:pointer" data-jump="${escapeHtml(ca.code)}">
                ${escapeHtml(ca.code)} ${escapeHtml(ca.name)}
              </strong>
              <span class="mono"> 現價 ${nowP} ｜ 條件 ${escapeHtml(ca.operator)} <strong>${fmtPrice(ca.targetPrice)}</strong> 元</span>
              ${isHit ? '<span class="inline-alert-tag" style="margin-left:6px">已達標觸發 🚨</span>' : ""}
              ${ca.note ? `<div style="font-size:11px;color:var(--text-secondary)">${escapeHtml(ca.note)}</div>` : ""}
            </div>
            <button class="ca-del-btn" data-del-id="${escapeHtml(ca.id)}">刪除</button>
          </div>
        `;
      })
      .join("");
  }

  function updateCustomFormHint() {
    const code = normalizeSearch(document.getElementById("caCodeInput").value || "");
    const st = STATE.stockMap.get(code);
    const hintEl = document.getElementById("caCurrentPriceHint");
    const ll =
      (STATE.selectedDetail && STATE.selectedDetail.code === code && STATE.selectedDetail.tech && STATE.selectedDetail.tech.lifeline) ??
      (st && st.lifeline);
    if (st && st.price != null) {
      const llTxt = ll != null ? ` ｜ 🧬 α-生命線：${fmtPrice(ll)} 元` : "";
      hintEl.textContent = `${st.code} ${st.name} 現價：${fmtPrice(st.price)} 元${llTxt}`;
    } else {
      hintEl.textContent = "輸入有效台股代號即可帶入現價與生命線防守價";
    }
  }

  // ==================== 全市場 2,774 檔選股總表 ====================
  function renderScreenerCounts() {
    const all = STATE.stocks.length;
    let twse = 0, tpex = 0, esb = 0, alertStocks = 0;
    for (const s of STATE.stocks) {
      if (s.market === "TWSE") twse++;
      else if (s.market === "TPEX") tpex++;
      else if (s.market === "ESB") esb++;
      if ((s.alertCount || 0) > 0) alertStocks++;
    }
    document.getElementById("cntAll").textContent = fmtInt(all);
    document.getElementById("cntTwse").textContent = fmtInt(twse);
    document.getElementById("cntTpex").textContent = fmtInt(tpex);
    document.getElementById("cntEsb").textContent = fmtInt(esb);
    document.getElementById("cntAlertStocks").textContent = fmtInt(alertStocks);
    document.getElementById("cntWatch").textContent = fmtInt(STATE.watchlist.size);
  }

  function getFilteredSortedStocks() {
    const mkt = STATE.screenerMarket;
    const q = stripUncommittedZhuyin(STATE.screenerQuery);

    const arr = STATE.stocks.filter((s) => {
      if (mkt === "TWSE" && s.market !== "TWSE") return false;
      if (mkt === "TPEX" && s.market !== "TPEX") return false;
      if (mkt === "ESB" && s.market !== "ESB") return false;
      if (mkt === "ETF" && !s.isEtf) return false;
      if (mkt === "ALERT" && !(s.alertCount > 0)) return false;
      if (mkt === "WATCH" && !STATE.watchlist.has(s.code)) return false;
      if (q && !normalizeSearch(s.code).includes(q) && !normalizeSearch(s.name).includes(q)) return false;
      return true;
    });

    const key = STATE.sortKey;
    const dir = STATE.sortDir === "asc" ? 1 : -1;
    const sortVal = (item) => {
      if (key === "lifelineBias") {
        if (item.lifeline != null && item.lifeline > 0 && item.price != null) {
          return ((item.price - item.lifeline) / item.lifeline) * 100;
        }
        return item.lifelineBias ?? -Infinity;
      }
      return item[key] ?? -Infinity;
    };
    arr.sort((a, b) => {
      if (key === "code") {
        return a.code.localeCompare(b.code) * dir;
      }
      const va = sortVal(a);
      const vb = sortVal(b);
      if (va === vb) return 0;
      return (va > vb ? 1 : -1) * dir;
    });
    return arr;
  }

  function renderScreenerTable() {
    const filtered = getFilteredSortedStocks();
    const totalPages = Math.max(1, Math.ceil(filtered.length / STATE.pageSize));
    if (STATE.page > totalPages) STATE.page = totalPages;
    if (STATE.page < 1) STATE.page = 1;

    const start = (STATE.page - 1) * STATE.pageSize;
    const pageRows = filtered.slice(start, start + STATE.pageSize);

    document.getElementById("pageSummaryText").textContent =
      filtered.length > 0
        ? `顯示第 ${fmtInt(start + 1)}–${fmtInt(start + pageRows.length)} 檔，共 ${fmtInt(filtered.length)} 檔個股`
        : "查無符合篩選條件的個股";
    document.getElementById("pageIndicator").textContent = `${STATE.page} / ${totalPages}`;
    document.getElementById("prevPageBtn").disabled = STATE.page <= 1;
    document.getElementById("nextPageBtn").disabled = STATE.page >= totalPages;

    const tbody = document.getElementById("screenerTableBody");
    tbody.innerHTML = pageRows
      .map((s) => {
        const cls = priceColorClass(s.change);
        const isLu = s.limitUp && s.price >= s.limitUp - 1e-6;
        const isLd = s.limitDown && s.price <= s.limitDown + 1e-6;
        let pctCls = "";
        if (isLu || s.changePct >= 9.7) pctCls = "pct-badge pct-lu";
        else if (isLd || s.changePct <= -9.7) pctCls = "pct-badge pct-ld";
        else if (s.changePct > 0) pctCls = "pct-badge pct-up";
        else if (s.changePct < 0) pctCls = "pct-badge pct-down";

        const mktCls = s.market === "TPEX" ? "tpex" : s.market === "ESB" ? "esb" : "";
        const badgesHtml = (s.alertBadges || [])
          .map((b) => `<span class="inline-alert-tag">${escapeHtml(b)}</span>`)
          .join("");

        let lifelineCellHtml = '<span style="color:var(--text-muted);font-size:11px">點擊載入</span>';
        if (s.lifeline != null && s.lifeline > 0) {
          const bias = s.price != null ? ((s.price - s.lifeline) / s.lifeline) * 100 : s.lifelineBias;
          const biasCls = priceColorClass(bias);
          let sigMini = "";
          if (s.lifelineSignal === "FLIP_BULL") sigMini = ' <span style="color:#ff4d5e;font-weight:700">★翻多</span>';
          else if (s.lifelineSignal === "PULLBACK_HOLD") sigMini = ' <span style="color:#fbbf24;font-weight:700">★守線</span>';
          else if (s.lifelineSignal === "BREAKDOWN") sigMini = ' <span style="color:#10b981;font-weight:700">▼破線</span>';
          lifelineCellHtml = `<span style="color:#ffbf69;font-weight:600">${fmtPrice(s.lifeline)}</span> <span class="${biasCls}" style="font-size:11.5px">(${fmtSigned(bias, 1)}%)</span>${sigMini}`;
        }

        return `
          <tr data-code="${escapeHtml(s.code)}" class="${s.code === STATE.selectedCode ? "selected-row" : ""}">
            <td>
              <strong style="color:var(--accent-cyan)">${escapeHtml(s.code)}</strong>
              <span class="td-stock-name">${escapeHtml(s.name)}</span>
            </td>
            <td><span class="market-tag ${mktCls}">${escapeHtml(s.marketLabel)}</span></td>
            <td class="num ${cls}" style="font-weight:700">${fmtPrice(s.price)}</td>
            <td class="num ${cls}">${s.change != null ? fmtSigned(s.change, 2) : "--"}</td>
            <td class="num"><span class="${pctCls}">${fmtSigned(s.changePct, 2)}%</span></td>
            <td class="num">${fmtInt(s.volumeLots)}</td>
            <td class="num">${fmtNum(s.turnoverM, 1)}</td>
            <td class="num">${lifelineCellHtml}</td>
            <td class="num" style="color:#fde047;font-weight:700">${s.quantScore ?? "--"}</td>
            <td>${badgesHtml || '<span style="color:var(--text-muted);font-size:11px">--</span>'}</td>
          </tr>
        `;
      })
      .join("");
  }

  // ==================== 全市場即時搜尋下拉選單（支援手機注音選字、臺/台異體字、全形數字、後端容錯） ====================
  function buildSearchDropdownHtml(list) {
    return list
      .map((s, idx) => {
        const cls = priceColorClass(s.change);
        const mktCls = s.market === "TPEX" ? "tpex" : s.market === "ESB" ? "esb" : "";
        const llBadge =
          s.lifeline != null
            ? `<span class="mono" style="font-size:11px;color:#ffbf69">生命線 ${fmtPrice(s.lifeline)}</span>`
            : "";
        return `
          <div class="search-item ${idx === 0 ? "active" : ""}" data-code="${escapeHtml(s.code)}">
            <div class="si-left">
              <span class="market-tag ${mktCls}">${escapeHtml(s.marketLabel || s.market)}</span>
              <span class="si-code mono">${escapeHtml(s.code)}</span>
              <span class="si-name">${escapeHtml(s.name)}</span>
              ${s.alertCount > 0 ? `<span class="inline-alert-tag">🚨 ${s.alertCount}</span>` : llBadge}
            </div>
            <div class="si-right mono">
              <span class="si-price ${cls}">${fmtPrice(s.price)} 元</span>
              <span class="${cls}">${fmtSigned(s.changePct, 2)}%</span>
            </div>
          </div>
        `;
      })
      .join("");
  }

  async function renderSearchDropdown(rawQuery, fromCompositionEnd = false) {
    const dropdown = document.getElementById("searchDropdown");
    const clearBtn = document.getElementById("searchClearBtn");
    const rawStr = String(rawQuery || "");
    if (clearBtn) clearBtn.classList.toggle("hidden", rawStr.length === 0);

    const hasZhuyin = HAS_ZHUYIN_RE.test(rawStr);
    const q = stripUncommittedZhuyin(rawStr);

    if (!rawStr.trim()) {
      dropdown.classList.add("hidden");
      return;
    }

    // 手機注音輸入尚未選出第一個國字時，顯示友善選字提示而非「找不到」
    if (!q && (STATE.isSearchComposing || hasZhuyin) && !fromCompositionEnd) {
      dropdown.innerHTML = `<div class="ob-empty" style="padding:12px;">⌨️ 中文選字輸入中，點選國字或按「查詢」立即顯示現價...</div>`;
      dropdown.classList.remove("hidden");
      return;
    }

    if (!q) {
      dropdown.classList.add("hidden");
      return;
    }

    // 若行動網路尚在載入全市場清單，自動改走後端極速 /api/search
    if (STATE.stocks.length === 0) {
      const seq = ++STATE.searchFetchSeq;
      try {
        const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
        if (seq !== STATE.searchFetchSeq || !res.ok) return;
        const data = await res.json();
        const remoteItems = (data && (data.results || data.items)) || [];
        STATE.searchHighlightIdx = 0;
        if (remoteItems.length === 0) {
          dropdown.innerHTML = `<div class="ob-empty" style="padding:14px;">找不到符合「${escapeHtml(rawStr.trim())}」的台股代號或名稱</div>`;
        } else {
          dropdown.innerHTML = buildSearchDropdownHtml(remoteItems);
        }
        dropdown.classList.remove("hidden");
      } catch (_) {}
      return;
    }

    const matches = [];
    for (const s of STATE.stocks) {
      const nc = normalizeSearch(s.code);
      const nn = normalizeSearch(s.name);
      if (nc.includes(q) || nn.includes(q)) {
        matches.push(s);
      }
    }
    matches.sort((a, b) => {
      const ac = normalizeSearch(a.code), an = normalizeSearch(a.name);
      const bc = normalizeSearch(b.code), bn = normalizeSearch(b.name);
      const aRank = ac === q || an === q ? 0 : ac.startsWith(q) || an.startsWith(q) ? 1 : 2;
      const bRank = bc === q || bn === q ? 0 : bc.startsWith(q) || bn.startsWith(q) ? 1 : 2;
      if (aRank !== bRank) return aRank - bRank;
      return (b.turnoverM || 0) - (a.turnoverM || 0);
    });

    const top = matches.slice(0, 14);
    STATE.searchHighlightIdx = 0;

    if (top.length === 0) {
      if (STATE.isSearchComposing || hasZhuyin) {
        dropdown.innerHTML = `<div class="ob-empty" style="padding:12px;">⌨️ 中文選字中...</div>`;
      } else {
        dropdown.innerHTML = `<div class="ob-empty" style="padding:14px;">找不到符合「${escapeHtml(rawStr.trim())}」的台股代號或名稱</div>`;
      }
      dropdown.classList.remove("hidden");
      return;
    }

    dropdown.innerHTML = buildSearchDropdownHtml(top);
    dropdown.classList.remove("hidden");
  }

  // ==================== SSE 即時串流連線 ====================
  function connectSSE() {
    const es = new EventSource("/api/stream");

    es.addEventListener("tick", (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        STATE.mode = payload.mode || STATE.mode;
        STATE.marketOpen = Boolean(payload.marketOpen);
        if (payload.indices) STATE.indices = payload.indices;

        const updatedStocks = payload.stocks || [];
        let selectedTicked = false;
        let selectedFlashDir = 0;

        for (const u of updatedStocks) {
          const prev = STATE.stockMap.get(u.code);
          if (prev) {
            if (u.code === STATE.selectedCode && prev.price != null && u.price != null && u.price !== prev.price) {
              selectedFlashDir = u.price > prev.price ? 1 : -1;
            }
            Object.assign(prev, u);
          }
          if (u.code === STATE.selectedCode) {
            selectedTicked = true;
          }
        }

        renderHeaderAndPulse({ lastQuoteSync: payload.lastQuoteSync });
        renderScreenerCounts();
        renderScreenerTable();

        // 若當前檢視中的個股有更新，即時刷新現價、生命線位階與五檔
        if (selectedTicked) {
          const curSnap = STATE.stockMap.get(STATE.selectedCode);
          if (curSnap) {
            renderStockHeroBasic(curSnap, selectedFlashDir);
            renderLifelineStrip(STATE.selectedDetail ? { ...STATE.selectedDetail, price: curSnap.price } : curSnap);
          }
          if (STATE.mode === "SIM") {
            fetch(`/api/stock/${encodeURIComponent(STATE.selectedCode)}?interval=${STATE.klineInterval}`)
              .then((r) => (r.ok ? r.json() : null))
              .then((det) => {
                if (det && det.code === STATE.selectedCode) {
                  STATE.selectedDetail = det;
                  renderLifelineStrip(det);
                  renderOrderbook(det);
                  renderFactorDiagnosis(det);
                }
              })
              .catch(() => {});
          }
        }

        // 新觸發的量化警示：加入雷達、彈窗通知、播放音效
        const newAlerts = payload.newAlerts || [];
        if (newAlerts.length > 0) {
          for (const na of newAlerts) {
            STATE.alerts.unshift(na);
            pushToastAlert(na);
          }
          STATE.alerts = STATE.alerts.slice(0, 250);
          STATE.totalAlertCount += newAlerts.length;
          playAlertChime(newAlerts.some((x) => x.category === "RISK"));
          renderAlertRadar();
          renderCustomAlertsList();
        }
      } catch (e) {
        console.error("SSE tick parse error:", e);
      }
    });

    es.addEventListener("alerts_sync", (ev) => {
      try {
        const data = JSON.parse(ev.data);
        STATE.alerts = data.alerts || [];
        STATE.totalAlertCount = data.totalAlerts ?? STATE.alerts.length;
        if (data.ruleHitCounts) {
          STATE.ruleHitCounts = data.ruleHitCounts;
          Object.entries(data.ruleHitCounts).forEach(([rid, cnt]) => {
            const el = document.getElementById(`hit_${rid}`);
            if (el) el.textContent = `觸發 ${cnt} 檔`;
          });
        }
        if (data.stockBadges) {
          for (const st of STATE.stocks) {
            const b = data.stockBadges[st.code] || [];
            st.alertBadges = b;
            st.alertCount = b.length;
          }
        }
        if (data.newAlerts && data.newAlerts.length > 0) {
          pushToastAlert(data.newAlerts[0]);
          playAlertChime(data.newAlerts[0].category === "RISK");
        }
        renderAlertRadar();
        renderCustomAlertsList();
        renderScreenerCounts();
        renderScreenerTable();
      } catch (e) {
        console.error("SSE alerts_sync error:", e);
      }
    });

    es.onerror = () => {
      setTimeout(() => {
        if (es.readyState === EventSource.CLOSED) connectSSE();
      }, 4000);
    };
  }

  // ==================== 事件綁定 ====================
  function bindEvents() {
    const searchForm = document.getElementById("globalSearchForm");
    const searchInput = document.getElementById("globalSearchInput");
    const clearBtn = document.getElementById("searchClearBtn");
    const dropdown = document.getElementById("searchDropdown");

    const commitSearchSelection = (codeOrQuery) => {
      if (!codeOrQuery) return;
      dropdown.classList.add("hidden");
      searchInput.value = "";
      if (clearBtn) clearBtn.classList.add("hidden");
      searchInput.blur();
      selectStock(codeOrQuery, true);
    };

    searchInput.addEventListener("compositionstart", () => {
      STATE.isSearchComposing = true;
    });
    searchInput.addEventListener("compositionend", () => {
      STATE.isSearchComposing = false;
      renderSearchDropdown(searchInput.value, true);
    });
    searchInput.addEventListener("input", () => {
      renderSearchDropdown(searchInput.value, false);
    });
    searchInput.addEventListener("focus", () => {
      if (searchInput.value.trim()) renderSearchDropdown(searchInput.value, false);
    });

    if (clearBtn) {
      const clearHandler = (e) => {
        e.preventDefault();
        e.stopPropagation();
        searchInput.value = "";
        clearBtn.classList.add("hidden");
        dropdown.classList.add("hidden");
        searchInput.focus();
      };
      clearBtn.addEventListener("mousedown", (e) => e.preventDefault());
      clearBtn.addEventListener("click", clearHandler);
    }

    // 支援手機虛擬鍵盤「搜尋 / 前往」鍵與「查詢」按鈕直接送出
    if (searchForm) {
      searchForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const items = Array.from(dropdown.querySelectorAll(".search-item"));
        const activeItem = !dropdown.classList.contains("hidden")
          ? items[STATE.searchHighlightIdx] || items[0]
          : null;
        if (activeItem && activeItem.dataset.code) {
          commitSearchSelection(activeItem.dataset.code);
          return;
        }
        const cleaned = stripUncommittedZhuyin(searchInput.value) || normalizeSearch(searchInput.value);
        if (cleaned) {
          commitSearchSelection(cleaned);
        } else {
          searchInput.focus();
        }
      });
    }

    searchInput.addEventListener("keydown", (e) => {
      // 中文輸入法選字期間絕對不可攔截 Enter 或上下鍵
      if (e.isComposing || STATE.isSearchComposing || e.keyCode === 229) return;

      const items = Array.from(dropdown.querySelectorAll(".search-item"));
      if (e.key === "ArrowDown" && items.length > 0) {
        e.preventDefault();
        STATE.searchHighlightIdx = (STATE.searchHighlightIdx + 1) % items.length;
        items.forEach((el, i) => el.classList.toggle("active", i === STATE.searchHighlightIdx));
        items[STATE.searchHighlightIdx].scrollIntoView({ block: "nearest" });
      } else if (e.key === "ArrowUp" && items.length > 0) {
        e.preventDefault();
        STATE.searchHighlightIdx = (STATE.searchHighlightIdx - 1 + items.length) % items.length;
        items.forEach((el, i) => el.classList.toggle("active", i === STATE.searchHighlightIdx));
        items[STATE.searchHighlightIdx].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Escape") {
        dropdown.classList.add("hidden");
        searchInput.blur();
      }
    });

    // 防止電腦與手機點擊下拉選單時因 input blur 收合鍵盤造成位移漏點
    dropdown.addEventListener("mousedown", (e) => {
      if (e.target.closest(".search-item")) {
        e.preventDefault();
      }
    });

    let ddTouchStartX = 0;
    let ddTouchStartY = 0;
    dropdown.addEventListener(
      "touchstart",
      (e) => {
        if (e.touches && e.touches[0]) {
          ddTouchStartX = e.touches[0].clientX;
          ddTouchStartY = e.touches[0].clientY;
        }
      },
      { passive: true }
    );
    dropdown.addEventListener("touchend", (e) => {
      const item = e.target.closest(".search-item");
      if (!item || !item.dataset.code || !e.changedTouches || !e.changedTouches[0]) return;
      const dx = e.changedTouches[0].clientX - ddTouchStartX;
      const dy = e.changedTouches[0].clientY - ddTouchStartY;
      // 手指位移小於 12px 視為點擊選股（非滑動捲動選單）
      if (Math.hypot(dx, dy) < 12) {
        e.preventDefault();
        commitSearchSelection(item.dataset.code);
      }
    });

    dropdown.addEventListener("click", (e) => {
      const item = e.target.closest(".search-item");
      if (item && item.dataset.code) {
        commitSearchSelection(item.dataset.code);
      }
    });

    document.addEventListener("click", (e) => {
      if (!e.target.closest("#searchWrapper")) {
        dropdown.classList.add("hidden");
      }
    });

    // 全域快捷鍵 /
    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
        e.preventDefault();
        searchInput.focus();
      }
    });

    // 快捷熱門股按鈕
    document.getElementById("quickChips").addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (chip && chip.dataset.code) selectStock(chip.dataset.code, false);
    });

    // 模擬盤中跳動按鈕
    document.getElementById("simToggleBtn").addEventListener("click", async () => {
      const nextMode = STATE.mode !== "SIM";
      const res = await fetch("/api/sim/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: nextMode }),
      });
      const data = await res.json();
      applyBootstrapData(data);
      selectStock(STATE.selectedCode, false);
      if (nextMode) {
        pushToastAlert({
          category: "MICRO",
          badge: "模擬撮合啟動 ⚡",
          code: STATE.selectedCode,
          name: (STATE.stockMap.get(STATE.selectedCode) || {}).name || "",
          price: (STATE.stockMap.get(STATE.selectedCode) || {}).price || 0,
          changePct: (STATE.stockMap.get(STATE.selectedCode) || {}).changePct || 0,
          reason: "已啟動台股升降單位動態撮合模擬，每 1.6 秒即時更新股價與五檔並觸發頁面條件警示！",
        });
      }
    });

    // 音效切換
    const soundBtn = document.getElementById("soundToggleBtn");
    soundBtn.addEventListener("click", () => {
      STATE.soundEnabled = !STATE.soundEnabled;
      soundBtn.classList.toggle("active", STATE.soundEnabled);
      soundBtn.textContent = STATE.soundEnabled ? "🔔 音效：開" : "🔕 音效：關";
      if (STATE.soundEnabled) playAlertChime(false);
    });

    // 同步全市場
    document.getElementById("refreshAllBtn").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.textContent = "⏳ 同步中...";
      try {
        const res = await fetch("/api/refresh", { method: "POST", body: "{}" });
        const data = await res.json();
        applyBootstrapData(data);
        await selectStock(STATE.selectedCode, false);
      } finally {
        btn.textContent = "🔄 同步全市場";
      }
    });

    // 自選股按鈕
    document.getElementById("watchStarBtn").addEventListener("click", () => {
      const c = STATE.selectedCode;
      if (STATE.watchlist.has(c)) STATE.watchlist.delete(c);
      else STATE.watchlist.add(c);
      try {
        window.localStorage.setItem("tw_quant_watchlist", JSON.stringify(Array.from(STATE.watchlist)));
      } catch (_) {}
      const snap = STATE.stockMap.get(c);
      if (snap) renderStockHeroBasic(snap);
      renderScreenerCounts();
      if (STATE.screenerMarket === "WATCH") renderScreenerTable();
    });

    // 立即更新此股現價
    document.getElementById("forceRefreshStockBtn").addEventListener("click", () => {
      selectStock(STATE.selectedCode, false);
    });

    // 跳轉設定自訂到價警示
    document.getElementById("openCustomAlertForStockBtn").addEventListener("click", () => {
      switchRulesTab("custom");
      const codeInput = document.getElementById("caCodeInput");
      codeInput.value = STATE.selectedCode;
      updateCustomFormHint();
      document.getElementById("rulesConfigSection").scrollIntoView({ behavior: "smooth", block: "center" });
      document.getElementById("caPriceInput").focus();
    });

    document.getElementById("jumpToRulesBtn").addEventListener("click", () => {
      switchRulesTab("sys");
      document.getElementById("rulesConfigSection").scrollIntoView({ behavior: "smooth", block: "center" });
    });

    // K 線週期切換
    document.getElementById("klineIntervalTabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn || !btn.dataset.interval) return;
      STATE.klineInterval = btn.dataset.interval;
      document.querySelectorAll("#klineIntervalTabs .seg-btn").forEach((b) =>
        b.classList.toggle("active", b === btn)
      );
      selectStock(STATE.selectedCode, false);
    });

    // K 棒顯示根數縮放切換 (38根放大 / 55根標準 / 85根全貌)
    const zoomTabsEl = document.getElementById("klineZoomTabs");
    if (zoomTabsEl) {
      zoomTabsEl.addEventListener("click", (e) => {
        const btn = e.target.closest(".seg-btn");
        if (!btn || !btn.dataset.bars) return;
        const bars = parseInt(btn.dataset.bars, 10);
        if (bars > 0) {
          STATE.klineBarCount = bars;
          STATE.hoverIndex = null;
          zoomTabsEl.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
          renderKlineCanvas();
        }
      });
    }

    // 副圖指標切換
    document.getElementById("subChartTabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (!btn || !btn.dataset.sub) return;
      STATE.subChartType = btn.dataset.sub;
      document.querySelectorAll("#subChartTabs .seg-btn").forEach((b) =>
        b.classList.toggle("active", b === btn)
      );
      renderKlineCanvas();
    });

    // K線滑鼠與手機觸控十字線互動
    const canvas = document.getElementById("klineCanvas");
    const updateCrosshairByClientX = (clientX) => {
      const meta = canvas._chartMeta;
      if (!meta) return;
      const r = canvas.getBoundingClientRect();
      const x = clientX - r.left;
      const ratio = (x - meta.padL) / meta.plotW;
      if (ratio >= 0 && ratio <= 1) {
        STATE.hoverIndex = Math.min(meta.count - 1, Math.max(0, Math.floor(ratio * meta.count)));
      } else {
        STATE.hoverIndex = null;
      }
      renderKlineCanvas();
    };
    canvas.addEventListener("mousemove", (e) => updateCrosshairByClientX(e.clientX));
    canvas.addEventListener("mouseleave", () => {
      STATE.hoverIndex = null;
      renderKlineCanvas();
    });
    canvas.addEventListener("touchstart", (e) => {
      if (e.touches && e.touches[0]) updateCrosshairByClientX(e.touches[0].clientX);
    }, { passive: true });
    canvas.addEventListener("touchmove", (e) => {
      if (e.touches && e.touches[0]) updateCrosshairByClientX(e.touches[0].clientX);
    }, { passive: true });
    canvas.addEventListener("touchend", () => {
      STATE.hoverIndex = null;
      renderKlineCanvas();
    });
    window.addEventListener("resize", () => renderKlineCanvas());

    // iOS / Android 首次點擊螢幕時自動解鎖 WebAudio 音效引擎
    const unlockAudio = () => {
      try {
        const AudioContextCls = window.AudioContext || window.webkitAudioContext;
        if (AudioContextCls && !STATE.audioCtx) {
          STATE.audioCtx = new AudioContextCls();
        }
        if (STATE.audioCtx && STATE.audioCtx.state === "suspended") {
          STATE.audioCtx.resume();
        }
      } catch (_) {}
      document.removeEventListener("touchstart", unlockAudio);
      document.removeEventListener("click", unlockAudio);
    };
    document.addEventListener("touchstart", unlockAudio, { passive: true });
    document.addEventListener("click", unlockAudio);

    // 手機連線 QR Code 與網址彈窗
    const mobileModal = document.getElementById("mobileModal");
    const mobileBtn = document.getElementById("mobileConnectBtn");
    const closeMobileBtn = document.getElementById("closeMobileModalBtn");
    if (mobileBtn && mobileModal) {
      mobileBtn.addEventListener("click", async () => {
        mobileModal.classList.remove("hidden");
        try {
          const res = await fetch("/api/mobile-info");
          const info = await res.json();
          const pubLink = document.getElementById("mobilePublicLink");
          const lanLink = document.getElementById("mobileLanLink");
          const pubBlock = document.getElementById("pubUrlBlock");
          const qrImg = document.getElementById("mobileQrImg");

          lanLink.textContent = info.lanUrl;
          lanLink.href = info.lanUrl;

          const bestUrl = info.publicUrl || info.lanUrl;
          if (info.publicUrl) {
            pubBlock.classList.remove("hidden");
            pubLink.textContent = info.publicUrl;
            pubLink.href = info.publicUrl;
          } else {
            pubBlock.classList.add("hidden");
          }
          qrImg.src = `https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${encodeURIComponent(bestUrl)}`;
        } catch (err) {
          console.error("Mobile info error:", err);
        }
      });
      closeMobileBtn.addEventListener("click", () => mobileModal.classList.add("hidden"));
      mobileModal.addEventListener("click", (e) => {
        if (e.target === mobileModal) mobileModal.classList.add("hidden");
      });
    }

    // 警示分類過濾
    document.getElementById("alertCategoryPills").addEventListener("click", (e) => {
      const btn = e.target.closest(".f-pill");
      if (!btn) return;
      STATE.alertCategoryFilter = btn.dataset.cat;
      document.querySelectorAll("#alertCategoryPills .f-pill").forEach((b) =>
        b.classList.toggle("active", b === btn)
      );
      renderAlertRadar();
    });

    // 點擊警示雷達卡片切換個股
    document.getElementById("alertFeedList").addEventListener("click", (e) => {
      const card = e.target.closest(".alert-card");
      if (card && card.dataset.code) {
        selectStock(card.dataset.code, true);
      }
    });

    // 策略面板 Tab 切換
    document.getElementById("tabSysRulesBtn").addEventListener("click", () => switchRulesTab("sys"));
    document.getElementById("tabCustomAlertBtn").addEventListener("click", () => switchRulesTab("custom"));

    // 儲存量化條件設定
    document.getElementById("saveRulesBtn").addEventListener("click", async () => {
      const updatedRules = STATE.rules.map((r) => ({ id: r.id, enabled: r.enabled, params: {} }));
      const mapById = Object.fromEntries(updatedRules.map((r) => [r.id, r]));

      document.querySelectorAll(".rule-enable-chk").forEach((chk) => {
        const rid = chk.dataset.ruleId;
        if (mapById[rid]) mapById[rid].enabled = chk.checked;
      });
      document.querySelectorAll("#rulesListContainer input[type='number']").forEach((inp) => {
        const rid = inp.dataset.ruleId;
        const pk = inp.dataset.paramKey;
        if (mapById[rid] && pk) {
          mapById[rid].params[pk] = { value: parseFloat(inp.value) };
        }
      });

      const res = await fetch("/api/rules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rules: updatedRules }),
      });
      const data = await res.json();
      if (data.ok) {
        STATE.rules = data.rules;
        STATE.ruleHitCounts = data.ruleHitCounts || {};
        STATE.alerts = data.alerts || [];
        STATE.totalAlertCount = data.totalAlertCount ?? STATE.alerts.length;
        renderRulesConfig();
        renderAlertRadar();
        selectStock(STATE.selectedCode, false);
        pushToastAlert({
          category: "TECH",
          badge: "策略已更新 ✅",
          code: "全市場",
          name: `${STATE.stocks.length}檔重新掃描完成`,
          price: (STATE.stockMap.get(STATE.selectedCode) || {}).price || 0,
          changePct: (STATE.stockMap.get(STATE.selectedCode) || {}).changePct || 0,
          reason: `最新量化條件共觸發 ${STATE.totalAlertCount} 筆警示訊號！`,
        });
      }
    });

    // 恢復預設量化條件
    document.getElementById("resetRulesBtn").addEventListener("click", async () => {
      const res = await fetch("/api/rules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reset: true }),
      });
      const data = await res.json();
      if (data.ok) {
        STATE.rules = data.rules;
        STATE.ruleHitCounts = data.ruleHitCounts || {};
        STATE.alerts = data.alerts || [];
        STATE.totalAlertCount = data.totalAlertCount ?? STATE.alerts.length;
        renderRulesConfig();
        renderAlertRadar();
      }
    });

    // 自訂到價警示輸入連動與快速帶入（含一鍵帶入自研 α-生命線防守價）
    document.getElementById("caCodeInput").addEventListener("input", updateCustomFormHint);
    document.querySelectorAll(".quick-pct-btns .mini-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const code = normalizeSearch(document.getElementById("caCodeInput").value || STATE.selectedCode);
        const st = STATE.stockMap.get(code);
        if (!st || !st.price) return;
        document.getElementById("caCodeInput").value = code;
        const mode = btn.dataset.fill;
        const pInput = document.getElementById("caPriceInput");
        const opSel = document.getElementById("caOpSelect");
        const noteInput = document.getElementById("caNoteInput");

        if (mode === "lifeline") {
          let ll =
            (STATE.selectedDetail && STATE.selectedDetail.code === code && STATE.selectedDetail.tech && STATE.selectedDetail.tech.lifeline) ??
            st.lifeline;
          if (ll == null) {
            try {
              const r = await fetch(`/api/stock/${encodeURIComponent(code)}?interval=1d`);
              if (r.ok) {
                const det = await r.json();
                ll = det?.tech?.lifeline ?? det?.kline?.latest?.lifeline ?? null;
                if (ll != null) st.lifeline = ll;
              }
            } catch (_) {}
          }
          if (ll != null) {
            pInput.value = ll;
            if (st.price >= ll) {
              opSel.value = "<=";
              if (noteInput && !noteInput.value) noteInput.value = "跌破自研 α-生命線防守停損警示";
            } else {
              opSel.value = ">=";
              if (noteInput && !noteInput.value) noteInput.value = "突破站上自研 α-生命線翻多警示";
            }
          } else {
            pInput.value = (st.price * 0.98).toFixed(2);
            opSel.value = "<=";
          }
        } else if (mode === "now") {
          pInput.value = st.price;
          opSel.value = ">=";
        } else if (mode === "+2%") {
          pInput.value = (st.price * 1.02).toFixed(2);
          opSel.value = ">=";
        }
        updateCustomFormHint();
      });
    });

    document.getElementById("customAlertForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      let code = normalizeSearch(document.getElementById("caCodeInput").value);
      if (!STATE.stockMap.has(code) && STATE.stocks.length > 0) {
        const byName = STATE.stocks.find((s) => normalizeSearch(s.name) === code);
        if (byName) code = byName.code;
      }
      const operator = document.getElementById("caOpSelect").value;
      const targetPrice = parseFloat(document.getElementById("caPriceInput").value);
      const note = document.getElementById("caNoteInput").value.trim();

      const res = await fetch("/api/price-alerts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "add", code, operator, targetPrice, note }),
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        alert(data.error || "新增失敗，請確認股票代號與價格");
        return;
      }
      STATE.customPriceAlerts = data.customPriceAlerts || [];
      STATE.alerts = data.alerts || [];
      renderCustomAlertsList();
      renderAlertRadar();
      document.getElementById("caNoteInput").value = "";
      if (code === STATE.selectedCode) selectStock(code, false);
    });

    document.getElementById("customAlertsList").addEventListener("click", async (e) => {
      const delBtn = e.target.closest("[data-del-id]");
      if (delBtn) {
        const res = await fetch("/api/price-alerts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "delete", id: delBtn.dataset.delId }),
        });
        const data = await res.json();
        STATE.customPriceAlerts = data.customPriceAlerts || [];
        STATE.alerts = data.alerts || [];
        renderCustomAlertsList();
        renderAlertRadar();
        return;
      }
      const jump = e.target.closest("[data-jump]");
      if (jump) selectStock(jump.dataset.jump, true);
    });

    // 全市場選股表過濾與排序
    document.getElementById("screenerMarketTabs").addEventListener("click", (e) => {
      const tab = e.target.closest(".m-tab");
      if (!tab) return;
      STATE.screenerMarket = tab.dataset.mkt;
      STATE.page = 1;
      document.querySelectorAll("#screenerMarketTabs .m-tab").forEach((t) =>
        t.classList.toggle("active", t === tab)
      );
      renderScreenerTable();
    });

    const tableFilterEl = document.getElementById("tableFilterInput");
    tableFilterEl.addEventListener("compositionstart", () => {
      STATE.isTableComposing = true;
    });
    tableFilterEl.addEventListener("compositionend", (e) => {
      STATE.isTableComposing = false;
      STATE.screenerQuery = e.target.value;
      STATE.page = 1;
      renderScreenerTable();
    });
    tableFilterEl.addEventListener("input", (e) => {
      STATE.screenerQuery = e.target.value;
      STATE.page = 1;
      renderScreenerTable();
    });

    document.querySelectorAll(".quant-table thead th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (STATE.sortKey === key) {
          STATE.sortDir = STATE.sortDir === "desc" ? "asc" : "desc";
        } else {
          STATE.sortKey = key;
          STATE.sortDir = key === "code" ? "asc" : "desc";
        }
        document.querySelectorAll(".quant-table thead th").forEach((h) => h.classList.remove("active-sort"));
        th.classList.add("active-sort");
        STATE.page = 1;
        renderScreenerTable();
      });
    });

    document.getElementById("screenerTableBody").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-code]");
      if (tr && tr.dataset.code) {
        selectStock(tr.dataset.code, true);
      }
    });

    document.getElementById("prevPageBtn").addEventListener("click", () => {
      if (STATE.page > 1) {
        STATE.page--;
        renderScreenerTable();
      }
    });
    document.getElementById("nextPageBtn").addEventListener("click", () => {
      STATE.page++;
      renderScreenerTable();
    });
  }

  function switchRulesTab(which) {
    const isSys = which === "sys";
    document.getElementById("tabSysRulesBtn").classList.toggle("active", isSys);
    document.getElementById("tabCustomAlertBtn").classList.toggle("active", !isSys);
    document.getElementById("sysRulesPane").classList.toggle("hidden", !isSys);
    document.getElementById("customAlertsPane").classList.toggle("hidden", isSys);
  }

  // ==================== 啟動入口（含雲端 Render 保活與手機背景切回自動同步） ====================
  async function init() {
    bindEvents();
    await loadBootstrap(true);
    await selectStock("2330", false);
    connectSSE();

    // 開啟頁面期間每 4 分鐘發送輕量心跳，防止 Render 免費方案看盤中途進入休眠
    setInterval(() => {
      fetch("/healthz").catch(() => {});
    }, 240000);

    // 手機鎖屏或切換 App 超過 20 秒切回畫面時，自動刷新最新報價與當前個股
    let hiddenAt = 0;
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
      } else if (document.visibilityState === "visible" && hiddenAt > 0 && Date.now() - hiddenAt > 20000) {
        hiddenAt = 0;
        loadBootstrap(false);
        if (STATE.selectedCode) selectStock(STATE.selectedCode, false);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
