(() => {
    const selector = document.getElementById('trafficSelector');
    let packs = selector ? Number(selector.dataset.currentPacks) : 0;
    window.getTrafficPacks = () => packs;
    if (selector) selector.addEventListener('click', event => {
        const button = event.target.closest('[data-traffic-packs]');
        if (!button) return;
        packs = Math.max(0, Math.min(Number(selector.dataset.maxPacks), packs + Number(button.dataset.trafficPacks)));
        document.getElementById('trafficPacksLabel').textContent = '+' + packs * Number(selector.dataset.packGb) + ' ГБ/мес';
        document.querySelectorAll('.tma-stars-btn').forEach(button => { button.hidden = packs > 0; });
        window.dispatchEvent(new Event('traffic-packs-changed'));
    });
    const box = document.getElementById('buyTrafficBox');
    if (!box) return;
    const pay = document.getElementById('buyTrafficBtn');
    const notice = document.getElementById('trafficPurchaseNotice');
    const source = location.pathname.startsWith('/tma') ? 'tma' : 'web';
    let quantity = 1, version = 0, quotedAmount = null, busy = false;
    async function request(path, body) {
        const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Не удалось оформить счёт');
        return data;
    }
    async function refresh() {
        const current = ++version;
        quotedAmount = null;
        pay.disabled = true;
        try {
            const data = await request('/payment/traffic/quote', {packs: quantity, source});
            if (current !== version) return;
            quotedAmount = data.final_amount;
            pay.textContent = 'Докупить за ' + Number(quotedAmount).toFixed(2) + ' ₽';
            pay.disabled = busy;
            notice.textContent = 'Лимит после оплаты: ' + data.total_limit_gb + ' ГБ/мес';
        } catch (error) { if (current === version) notice.textContent = error.message; }
    }
    box.addEventListener('click', event => {
        const button = event.target.closest('[data-buy-traffic]');
        if (!button || busy) return;
        quantity = Math.max(1, Math.min(Number(box.dataset.trafficAvailable), quantity + Number(button.dataset.buyTraffic)));
        document.getElementById('buyTrafficGb').textContent = '+' + quantity * Number(box.dataset.trafficPackGb) + ' ГБ';
        refresh();
    });
    pay.addEventListener('click', async () => {
        if (busy || quotedAmount === null) return;
        busy = true; pay.disabled = true;
        try {
            const quote = await request('/payment/traffic/quote', {packs: quantity, source});
            if (quote.final_amount !== quotedAmount && !confirm('Цена обновилась: ' + Number(quote.final_amount).toFixed(2) + ' ₽. Продолжить?')) return;
            const data = await request('/payment/traffic', {packs: quantity, source});
            if (data.final_amount !== quote.final_amount && !confirm('К оплате ' + Number(data.final_amount).toFixed(2) + ' ₽. Открыть счёт?')) return;
            if (source === 'tma' && window.Telegram && Telegram.WebApp) Telegram.WebApp.openLink(data.payment_url);
            else location.href = data.payment_url;
        } catch (error) { notice.textContent = error.message; }
        finally { busy = false; pay.disabled = quotedAmount === null; }
    });
    refresh();
})();
