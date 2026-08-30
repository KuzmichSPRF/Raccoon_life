
    let lang = localStorage.getItem('rl_lang') || 'ru';
    const translations = {
      ru: {
        t1: "🔺 Секретная Организация Енотов 🔺", t2: "Все игроки против Единого БОССА",
        s1: "Ваш урон", s2: "Ударов", s3: "Критов", s4: "Вы", s5: "БОСС", s6: "Энергия",
        msg: "Атакуй БОССА! Вместе мы сильнее!",
        b1: "⚔️ Удар (+20 эн)", b2: "🔥 Сильный (40)", b3: "💥 Ульта (80)", b4: "💚 Лечение (50)", b5: "🍪 100 HP (100 Шишек)",
        log: "Битва началась!",
        raidFighters: "бойцов", raidKills: "Убит", raidTimes: "раз", raidLead: "👑 Топ охотников за боссом",
        emptyLead: "Станьте первым, кто нанесет урон!"
      },
      en: {
        t1: "🔺 Secret Raccoon Organization 🔺", t2: "All players against the Global BOSS",
        s1: "Your Damage", s2: "Hits", s3: "Crits", s4: "You", s5: "BOSS", s6: "Energy",
        msg: "Attack the BOSS! Together we are stronger!",
        b1: "⚔️ Attack (+20 en)", b2: "🔥 Strong (40)", b3: "💥 Ult (80)", b4: "💚 Heal (50)", b5: "🍪 100 HP (100 Cones)",
        log: "Battle started!",
        raidFighters: "fighters", raidKills: "Defeated", raidTimes: "times", raidLead: "👑 Top Boss Hunters",
        emptyLead: "Be the first to deal damage!"
      }
    };
    let t = translations[lang] || translations.ru;

    function applyLang(selectedLang) {
      lang = selectedLang || 'ru';
      t = translations[lang] || translations.ru;
      const t1El = document.querySelector('.boss-title');
      if (t1El) t1El.innerText = t.t1;
      const t2El = document.querySelector('.boss-subtitle');
      if (t2El) t2El.innerText = t.t2;
      const labels = document.querySelectorAll('.label');
      if (labels.length >= 3) {
        labels[0].innerText = t.s1;
        labels[1].innerText = t.s2;
        labels[2].innerText = t.s3;
      }
      const pName = document.querySelector('.player-name');
      if (pName) pName.innerText = t.s4;
      const bName = document.querySelector('.boss-name');
      if (bName) bName.innerText = t.s5;
      const nrgText = document.getElementById('p-nrg-text');
      if (nrgText && nrgText.parentElement && nrgText.parentElement.childNodes[0]) {
        nrgText.parentElement.childNodes[0].nodeValue = t.s6 + " ";
      }
      const msgEl = document.getElementById('msg');
      if (msgEl && gameActive) msgEl.innerText = t.msg;
      const btnBasic = document.getElementById('btn-basic');
      if (btnBasic) btnBasic.innerText = t.b1;
      const sp1 = document.getElementById('btn-sp1');
      if (sp1) sp1.innerText = t.b2;
      const sp2 = document.getElementById('btn-sp2');
      if (sp2) sp2.innerText = t.b3;
      const sp3 = document.getElementById('btn-sp3');
      if (sp3) sp3.innerText = t.b4;
      const cookieBtn = document.getElementById('btn-cookie');
      if (cookieBtn) cookieBtn.innerText = t.b5;
      const leadTitle = document.getElementById('raid-lead-title');
      if (leadTitle) leadTitle.innerText = t.raidLead;
    }

    applyLang(lang);

    const API_URL = '/api/boss_hp';
    let bossHP = 1000000000;
    let bossMaxHP = 1000000000;
    let playerEnergy = 0;
    let playerHP = 100;
    let playerMaxHP = 100;
    let playerStats = { totalDamage: 0, hits: 0, crits: 0 };
    let gameActive = true;
    let tgUserId = null;
    let tgInitData = '';

    function getUserId() {
      try {
        const urlParams = new URLSearchParams(window.location.search);
        const u = urlParams.get('userId');
        if (u && !isNaN(parseInt(u, 10)) && parseInt(u, 10) > 0) return parseInt(u, 10);
      } catch (e) {}

      try {
        let id = localStorage.getItem('rl_user_id');
        if (id && id !== 'undefined' && id !== 'null') {
          const parsed = parseInt(id, 10);
          if (!isNaN(parsed) && parsed > 0) return parsed;
        }
      } catch (e) {}

      try {
        if (window.parent && window.parent.getUserId) {
          const pid = window.parent.getUserId();
          if (pid && !isNaN(parseInt(pid, 10)) && parseInt(pid, 10) > 0) return parseInt(pid, 10);
        }
      } catch (e) {}

      try {
        if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initDataUnsafe?.user?.id) {
          return window.Telegram.WebApp.initDataUnsafe.user.id;
        }
      } catch (e) {}

      try {
        let storedId = localStorage.getItem('rl_temp_user_id');
        if (!storedId) {
          storedId = String(Math.floor(Math.random() * 1000000) + 100000);
          localStorage.setItem('rl_temp_user_id', storedId);
        }
        return parseInt(storedId, 10);
      } catch (e) {
        return 999999;
      }
    }

    function getInitData() {
      if (tgInitData) return tgInitData;
      try {
        if (window.parent && window.parent.Telegram && window.parent.Telegram.WebApp && window.parent.Telegram.WebApp.initData) {
          return window.parent.Telegram.WebApp.initData;
        }
      } catch (e) {}
      try {
        if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) {
          return window.Telegram.WebApp.initData;
        }
      } catch (e) {}
      return '';
    }

    tgUserId = getUserId();
    tgInitData = getInitData();

    window.addEventListener('message', (e) => {
      if (!e.data) return;
      if (e.data.type === 'init_game') {
        if (e.data.userId) {
          tgUserId = parseInt(e.data.userId, 10);
          localStorage.setItem('rl_user_id', tgUserId);
        }
        if (e.data.initData) {
          tgInitData = e.data.initData;
        }
        if (e.data.lang) {
          applyLang(e.data.lang);
        }
        loadPlayerStats();
        loadBossHP();
      } else if (e.data.type === 'language_changed') {
        applyLang(e.data.lang);
      }
    });

    // Загрузка HP босса и данных рейда с сервера
    async function loadBossHP() {
      try {
        const res = await fetch(API_URL + '?t=' + Date.now(), { cache: 'no-store' });
        const data = await res.json();
        if (data.status === 'ok' && data.boss) {
            bossHP = data.boss.current_hp;
            bossMaxHP = data.boss.max_hp;
            updateBossHP();

            const fightersEl = document.getElementById('raid-fighters-count');
            const killsEl = document.getElementById('raid-kills-count');
            if (fightersEl) fightersEl.innerText = formatNumber(data.boss.total_fighters || 0);
            if (killsEl) killsEl.innerText = formatNumber(data.boss.kill_count || 0);

            if (data.boss.top_damagers && Array.isArray(data.boss.top_damagers)) {
              renderRaidTop(data.boss.top_damagers);
            }
        }
      } catch (e) {
        console.error('Ошибка загрузки HP босса:', e);
      }
    }

    function renderRaidTop(topList) {
      const container = document.getElementById('raid-top-list');
      if (!container) return;
      if (!topList || topList.length === 0) {
        container.innerHTML = `<div style="font-size: 0.72rem; color: var(--text-sec); text-align: center; padding: 6px;">${t.emptyLead}</div>`;
        return;
      }
      const medals = ['🥇', '🥈', '🥉', '4.', '5.'];
      container.innerHTML = topList.map((item, idx) => {
        const rankLabel = medals[idx] || (idx + 1) + '.';
        const isMe = tgUserId && String(item.user_id) === String(tgUserId);
        const nameStyle = isMe ? 'color: #ffd700; font-weight: 800;' : '';
        const badge = isMe ? ' <span style="font-size: 0.65rem; color: #ffd700;">(Вы)</span>' : '';
        return `
          <div class="raid-top-item" style="${isMe ? 'border: 1px solid rgba(255,215,0,0.4);' : ''}">
            <span class="raid-rank">${rankLabel}</span>
            <span class="raid-player-name" style="${nameStyle}">${escapeHTML(item.name)}${badge}</span>
            <span class="raid-dmg-val">${formatNumber(item.total_damage)}</span>
          </div>
        `;
      }).join('');
    }

    function escapeHTML(str) {
      if (!str) return '';
      return String(str).replace(/[&<>'"]/g, tag => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
      }[tag] || tag));
    }

    // Загрузка статистики игрока с сервера
    async function loadPlayerStats() {
      try {
        const uid = tgUserId || getUserId();
        if (!uid) return;
        const headers = {};
        const curInit = getInitData();
        if (curInit) headers['X-Telegram-Init-Data'] = curInit;

        const response = await fetch(`/api/player_stats?userId=${uid}&t=${Date.now()}`, { headers: headers });
        const data = await response.json();
        if (data.status === 'ok') {
          if (data.boss_damage) {
            playerStats.totalDamage = data.boss_damage.total_damage || 0;
            playerStats.hits = data.boss_damage.hits || 0;
          } else if (data.stats) {
            playerStats.totalDamage = data.stats.boss_damage || 0;
          }
          if (data.crits !== undefined) playerStats.crits = data.crits;
          updateStatsUI();
        }
      } catch (e) {
        console.error('Failed to load player stats:', e);
      }
    }

    function formatNumber(num) {
      if (num === undefined || num === null || isNaN(num)) return '0';
      return num.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    }

    function updateBossHP() {
      const percent = Math.max(0, (bossHP / bossMaxHP) * 100);
      const fillEl = document.getElementById('boss-hp-fill');
      const textEl = document.getElementById('boss-hp-text');
      if (fillEl) fillEl.style.width = percent + '%';
      if (textEl) textEl.innerText = formatNumber(Math.ceil(bossHP)) + ' HP';
    }

    function updatePlayerHP() {
      const percent = Math.max(0, (playerHP / playerMaxHP) * 100);
      const hpEl = document.getElementById('p-hp');
      const hpTextEl = document.getElementById('p-hp-text');
      if (hpEl) hpEl.style.width = percent + '%';
      if (hpTextEl) hpTextEl.innerText = Math.max(0, Math.floor(playerHP));
    }

    function updateStatsUI() {
      const dmgEl = document.getElementById('total-dmg');
      const hitsEl = document.getElementById('hits-count');
      const critEl = document.getElementById('crit-count');
      if (dmgEl) dmgEl.innerText = formatNumber(playerStats.totalDamage);
      if (hitsEl) hitsEl.innerText = formatNumber(playerStats.hits);
      if (critEl) critEl.innerText = formatNumber(playerStats.crits);
    }

    function updateEnergy() {
      const energyFill = document.getElementById('p-nrg');
      const energyText = document.getElementById('p-nrg-text');
      const btnBasic = document.getElementById('btn-basic');
      const btnStrong = document.getElementById('btn-sp1');
      const btnUlt = document.getElementById('btn-sp2');
      const btnHeal = document.getElementById('btn-sp3');
      const cookieBtn = document.getElementById('btn-cookie');

      if (btnBasic) btnBasic.disabled = !gameActive;
      if (cookieBtn) cookieBtn.disabled = !gameActive;

      if (energyFill) energyFill.style.width = Math.max(0, Math.min(100, playerEnergy)) + '%';
      if (energyText) energyText.innerText = Math.floor(playerEnergy);
      if (btnStrong) btnStrong.disabled = !gameActive || playerEnergy < 40;
      if (btnUlt) btnUlt.disabled = !gameActive || playerEnergy < 80;
      if (btnHeal) btnHeal.disabled = !gameActive || playerEnergy < 50;
    }

    function showDamageNumber(damage, isCrit, isPlayer) {
      const target = document.getElementById(isPlayer ? 'player-cap' : 'boss');
      if (!target) return;
      const rect = target.getBoundingClientRect();
      const el = document.createElement('div');
      el.className = 'damage-number';
      el.innerText = isCrit ? '💥' + damage : '-' + damage;
      el.style.left = (rect.left + rect.width / 2 + (Math.random() - 0.5) * 60) + 'px';
      el.style.top = (rect.top + rect.height / 2) + 'px';
      if (isCrit) { el.style.fontSize = '2rem'; el.style.color = '#ffd700'; }
      document.body.appendChild(el);
      setTimeout(() => el.remove(), 1000);
    }

    function buyCookie() {
      if (!gameActive) return;
      
      const uid = tgUserId || getUserId();
      if (!uid) {
        document.getElementById('log').innerText = lang === 'ru' ? "❌ Ошибка: нет ID пользователя." : "❌ Error: no user ID.";
        return;
      }
      
      const cookieBtn = document.getElementById('btn-cookie');
      if (cookieBtn) cookieBtn.disabled = true;
      document.getElementById('log').innerText = lang === 'ru' ? "🍪 Покупка печеньки..." : "🍪 Buying cookie...";

      const headers = { 'Content-Type': 'application/json' };
      const curInit = getInitData();
      if (curInit) headers['X-Telegram-Init-Data'] = curInit;

      fetch('/api/sync', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({
          type: 'spend_tokens',
          userId: uid,
          amount: 100,
          reason: 'cookie_heal'
        })
      })
      .then(r => r.json())
      .then(result => {
        if (result.status === 'ok') {
          if (result.tokens && result.tokens.balance !== undefined) {
            window.parent.postMessage({ type: 'token_balance', balance: result.tokens.balance }, '*');
          }

          playerHP = playerMaxHP;
          updatePlayerHP();
          document.getElementById('log').innerText = lang === 'ru' ? "🍪 100% HP восстановлено!" : "🍪 100% HP restored!";

          const cap = document.getElementById('player-cap');
          if (cap) {
            cap.classList.add('heal-effect');
            setTimeout(() => cap.classList.remove('heal-effect'), 400);
          }
        } else {
          const errorMsg = result.message || (lang === 'ru' ? 'Недостаточно шишек!' : 'Not enough cones!');
          document.getElementById('log').innerText = "❌ " + errorMsg;
        }
      })
      .catch(err => {
        console.error('❌ Network Error:', err);
        document.getElementById('log').innerText = lang === 'ru' ? "❌ Ошибка сети!" : "❌ Network error!";
      })
      .finally(() => {
        setTimeout(() => {
          if (cookieBtn && gameActive) cookieBtn.disabled = false;
        }, 500);
      });
    }

    async function attack(type) {
      if (!gameActive) return;

      if (type === 'strong' && playerEnergy < 40) return;
      if (type === 'ultimate' && playerEnergy < 80) return;
      if (type === 'heal' && playerEnergy < 50) return;

      let uid = tgUserId || getUserId();
      if (!uid) {
        uid = 999999;
        tgUserId = uid;
      }

      const buttons = document.querySelectorAll('.btn');
      buttons.forEach(btn => btn.disabled = true);
      document.getElementById('msg').innerText = lang === 'ru' ? "Атакуем..." : "Attacking...";

      try {
        const headers = { 'Content-Type': 'application/json' };
        const curInit = getInitData();
        if (curInit) headers['X-Telegram-Init-Data'] = curInit;

        const res = await fetch('/api/boss/attack', {
          method: 'POST',
          headers: headers,
          body: JSON.stringify({ userId: uid, action: type })
        });

        const data = await res.json();

        if (data.status === 'error' || data.error) {
            const errorMsg = data.message || data.error || (lang === 'ru' ? 'Сбой API' : 'API failure');
            console.error('❌ API Error:', errorMsg);
            document.getElementById('msg').innerText = (lang === 'ru' ? "❌ Ошибка: " : "❌ Error: ") + errorMsg;
            updateEnergy();
            return;
        }

        let damage = data.damage || 0;
        let heal = data.heal || 0;
        let isCrit = !!data.is_crit;
        let bossDamage = data.boss_damage || 0;
        let energyChange = data.energy_change || 0;
        let tokensEarned = data.tokens_earned || 0;
        let message = '';

        playerEnergy = Math.max(0, Math.min(100, playerEnergy + energyChange));

        if (type === 'basic') message = lang === 'ru' ? 'Обычный удар! +20 эн' : 'Basic attack! +20 en';
        else if (type === 'strong') message = lang === 'ru' ? 'Сильная атака! -40 эн' : 'Strong attack! -40 en';
        else if (type === 'ultimate') message = lang === 'ru' ? '💥 УЛЬТИМАТИВНАЯ АТАКА! -80 эн' : '💥 ULTIMATE ATTACK! -80 en';
        else if (type === 'heal') {
            playerHP = Math.min(playerMaxHP, playerHP + heal);
            message = (lang === 'ru' ? '💚 Лечение +' : '💚 Heal +') + heal + (lang === 'ru' ? ' HP! -50 эн' : ' HP! -50 en');
            updatePlayerHP();
        }

        if (type !== 'heal') {
          if (isCrit) {
            playerStats.crits = (playerStats.crits || 0) + 1;
            message = (lang === 'ru' ? '💥 КРИТ! ' : '💥 CRIT! ') + message;
          }
          if (data.boss_hp !== undefined) {
            bossHP = data.boss_hp;
          }
          playerStats.totalDamage = (playerStats.totalDamage || 0) + damage;
          playerStats.hits = (playerStats.hits || 0) + 1;

          showDamageNumber(damage, isCrit, false);
          const boss = document.getElementById('boss');
          if (boss) {
            boss.classList.remove('hit');
            void boss.offsetWidth;
            boss.classList.add('hit');
          }

          const playerCap = document.getElementById('player-cap');
          if (playerCap) {
            playerCap.classList.add('animate-attack');
            setTimeout(() => playerCap.classList.remove('animate-attack'), 400);
          }
        }

        if (tokensEarned > 0) {
          message += (lang === 'ru' ? ` (+${tokensEarned} Шишек!)` : ` (+${tokensEarned} Cones!)`);
        }

        updateStatsUI();
        updateBossHP();
        document.getElementById('msg').innerText = message;
        document.getElementById('log').innerText = (lang === 'ru' ? 'Энергия: ' : 'Energy: ') + Math.floor(playerEnergy) + ' | HP: ' + Math.floor(playerHP) + (lang === 'ru' ? ' | Урон: ' : ' | Damage: ') + (type === 'heal' ? '0' : damage);

        // Ответный удар босса
        if (bossDamage > 0) {
          setTimeout(() => {
            playerHP = Math.max(0, playerHP - bossDamage);
            updatePlayerHP();
            
            let counterMsg = (lang === 'ru' ? '💥 Босс ответил на ' : '💥 Boss countered for ') + bossDamage + (lang === 'ru' ? ' урона!' : ' damage!');
            document.getElementById('msg').innerText = message + "\n" + counterMsg;
            document.getElementById('log').innerText = (lang === 'ru' ? 'Энергия: ' : 'Energy: ') + Math.floor(playerEnergy) + ' | HP: ' + Math.floor(playerHP) + (lang === 'ru' ? ' | Урон: ' : ' | Damage: ') + (type === 'heal' ? '0' : damage);
            showDamageNumber(bossDamage, false, true);

            const bossEl = document.getElementById('boss');
            if (bossEl) {
              bossEl.classList.add('animate-boss-attack');
              setTimeout(() => bossEl.classList.remove('animate-boss-attack'), 300);
            }

            const playerCap = document.getElementById('player-cap');
            if (playerCap) {
              playerCap.classList.add('hit');
              setTimeout(() => playerCap.classList.remove('hit'), 300);
            }

            if (playerHP <= 0) {
              gameOver();
              return;
            }
            
            updateEnergy();
          }, 300);
        } else {
          updateEnergy();
        }

      } catch (err) {
        console.error('❌ Attack error:', err);
        document.getElementById('msg').innerText = lang === 'ru' ? "❌ Ошибка соединения!" : "❌ Connection error!";
        updateEnergy();
      }
    }

    function gameOver() {
      gameActive = false;
      
      document.getElementById('msg').innerText = lang === 'ru' ? '💀 ВЫ ПРОИГРАЛИ! Босс оказался сильнее...' : '💀 YOU LOST! The Boss was too strong...';
      document.getElementById('msg').style.color = 'var(--accent)';
      document.getElementById('log').innerText = lang === 'ru' ? 'Возврат в меню игры...' : 'Returning to menu...';

      document.querySelectorAll('.btn').forEach(btn => btn.disabled = true);

      setTimeout(() => {
        window.parent.postMessage({ type: 'game_over', game: 'boss' }, '*');
      }, 2000);
    }

    window.addEventListener('DOMContentLoaded', () => {
        console.log('=== BOSS RAID INIT ===');
        gameActive = true;
        
        loadPlayerStats();
        loadBossHP();
        setInterval(loadBossHP, 3000);
        
        updateEnergy();
        updatePlayerHP();
        updateStatsUI();
    });
  