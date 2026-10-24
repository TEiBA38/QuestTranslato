/**
 * ============================================================================
 * QuestTranslator Pro - Cloudflare Worker (순수 R2 전용 큐/커밋 동기화 서버)
 * ============================================================================
 * [특징]
 * 1. D1(SQL 데이터베이스) 의존성 100% 제거:
 *    - 하루 100,000행 쓰기 제한 영구 탈출! 행(Row) 제한이 전혀 없습니다.
 *    - 1만 개 단어를 커밋해도 R2 단일 파일 업로드(Class A 쓰기 1회)로 처리됩니다.
 * 2. Egress(데이터 다운로드 전송료) 평생 $0 무제한 무료.
 * 3. 동시 업로드 덮어쓰기 원천 방지 (Append-Only Log):
 *    - 각 유저의 커밋은 고유 타임스탬프와 UUID를 가진 독립 델타 파일로 R2에 저장됩니다.
 *    - 유저 여러 명이 동시에 푸시해도 파일명이 다르므로 절대 덮어써지지 않습니다.
 * 4. Git Pull 증분 동기화:
 *    - 클라이언트는 마지막으로 수신한 커밋 키(since) 이후에 생성된 신규 커밋 파일만 가볍게 Pull.
 *
 * ============================================================================
 * [Cloudflare 2분 초간단 세팅 방법 (D1 불필요!)]
 * ============================================================================
 * 1. R2 버킷 생성:
 *    - 좌측 메뉴 [R2 Object Storage] -> [Create bucket] -> 이름: `quest-translations`
 *
 * 2. Worker 생성 및 R2 바인딩 연결:
 *    - 좌측 메뉴 [Workers & Pages] -> [Create] -> [Create Worker] -> 이름: `quest-translator-sync` -> [Deploy]
 *    - 생성된 Worker의 [Settings] -> [Bindings] 탭에서 1개만 연결:
 *      [Add] -> [R2 bucket] -> Variable name: `R2_BUCKET` / R2 bucket: `quest-translations`
 *    - 상단 [Edit code] 버튼을 눌러 이 파일(`cloudflare_worker.js`)의 내용을 전체 붙여넣고 [Deploy] 클릭!
 *    - 발급된 `https://quest-translator-sync.내아이디.workers.dev` 주소를 `translation_memory.py`의 `CLOUDFLARE_WORKER_URL`에 입력하면 끝!
 * ============================================================================
 */

const VALID_CATEGORIES = new Set(["general", "items", "books"]);

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Content-Encoding, Authorization, If-None-Match",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    try {
      // ----------------------------------------------------------------------
      // 1. GET /master/:category -> R2에서 마스터 스냅샷(master_*.json.gz) 다운로드
      //    (ETag 캐싱 및 Cloudflare Edge Cache 적용으로 초고속/무료 응답)
      // ----------------------------------------------------------------------
      if (request.method === "GET" && path.startsWith("/master/")) {
        const category = path.replace("/master/", "").trim();
        if (!VALID_CATEGORIES.has(category)) {
          return new Response("Invalid category", { status: 400, headers: corsHeaders });
        }

        const key = `master_${category}.json.gz`;
        const object = await env.R2_BUCKET.get(key);
        if (!object) {
          return new Response("Not Found", { status: 404, headers: corsHeaders });
        }

        const clientEtag = request.headers.get("If-None-Match");
        if (clientEtag && clientEtag === object.httpEtag) {
          return new Response(null, {
            status: 304,
            headers: { ...corsHeaders, "ETag": object.httpEtag },
          });
        }

        return new Response(object.body, {
          status: 200,
          headers: {
            ...corsHeaders,
            "Content-Type": "application/gzip",
            "ETag": object.httpEtag,
            "Cache-Control": "public, max-age=300",
          },
        });
      }

      // ----------------------------------------------------------------------
      // 2. GET /pull?since=<last_key> -> 마지막 커밋 키 이후 추가된 신규 델타 커밋만 Pull
      //    (R2 commits/ 폴더의 신규 커밋 파일들을 시간순으로 최대 20개씩 병합 수신)
      // ----------------------------------------------------------------------
      if (request.method === "GET" && path === "/pull") {
        const sinceParam = (url.searchParams.get("since") || "").trim();
        const limit = Math.min(parseInt(url.searchParams.get("limit") || "20", 10) || 20, 30);

        const listOptions = {
          prefix: "commits/",
          limit: limit,
        };

        // sinceParam 처리 (신규 커밋 키: commits/0001..., 타임스탬프 숫자 등)
        if (sinceParam) {
          if (sinceParam.startsWith("commits/")) {
            listOptions.startAfter = sinceParam;
          } else if (/^\d{13,16}$/.test(sinceParam)) {
            listOptions.startAfter = `commits/${sinceParam.padStart(16, "0")}`;
          }
          // 레거시 D1의 작은 정수 ID인 경우 startAfter를 생략하여 commits/ 처음부터 읽음
        }

        const listed = await env.R2_BUCKET.list(listOptions);
        const commits = [];
        let lastKey = sinceParam;

        if (listed.objects && listed.objects.length > 0) {
          for (const obj of listed.objects) {
            lastKey = obj.key;
            try {
              const fileObj = await env.R2_BUCKET.get(obj.key);
              if (fileObj) {
                const data = await fileObj.json();
                if (Array.isArray(data.items)) {
                  for (const item of data.items) {
                    commits.push(item);
                  }
                }
              }
            } catch (readErr) {
              // 개별 커밋 파일 손상 시 무시하고 다음 파일 계속 처리
            }
          }
        }

        return new Response(
          JSON.stringify({
            latest_id: lastKey,
            has_more: listed.truncated,
            commits: commits,
          }),
          {
            status: 200,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          }
        );
      }

      // ----------------------------------------------------------------------
      // 3. POST /commit -> 클라이언트 큐(Queue)에 모인 신규 번역을 독립 R2 델타 파일로 저장
      //    (고유 타임스탬프+UUID 파일명이므로 동시 쓰기 덮어쓰기 0% 완벽 방지, D1 Row 제한 0)
      // ----------------------------------------------------------------------
      if (request.method === "POST" && path === "/commit") {
        let bodyText;
        const contentEncoding = request.headers.get("Content-Encoding") || "";
        if (contentEncoding.includes("gzip")) {
          const ds = new DecompressionStream("gzip");
          const decompressedStream = request.body.pipeThrough(ds);
          bodyText = await new Response(decompressedStream).text();
        } else {
          bodyText = await request.text();
        }

        const payload = JSON.parse(bodyText);
        const items = Array.isArray(payload.items) ? payload.items : [];
        if (items.length === 0) {
          return new Response(JSON.stringify({ ok: true, committed: 0, latest_id: "" }), {
            status: 200,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          });
        }

        const validItems = [];
        for (const row of items) {
          const cat = VALID_CATEGORIES.has(row.cat) ? row.cat : "general";
          const lang = String(row.lang || "한국어 (Korean)").trim();
          const src = String(row.src || "");
          const tgt = String(row.tgt || "");
          if (!src || !tgt || src === tgt) continue;
          validItems.push({ cat, lang, src, tgt });
        }

        if (validItems.length === 0) {
          return new Response(JSON.stringify({ ok: true, committed: 0, latest_id: "" }), {
            status: 200,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          });
        }

        // 고유 타임스탬프(16자리 0-padding) + 랜덤 UUID로 정렬 보장 및 파일명 충돌 원천 방지
        const now = Date.now();
        const padTime = now.toString().padStart(16, "0");
        const rand = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).substring(2)).replace(/-/g, "").slice(0, 8);
        const commitKey = `commits/${padTime}_${rand}.json`;

        await env.R2_BUCKET.put(
          commitKey,
          JSON.stringify({ timestamp: now, items: validItems }),
          {
            httpMetadata: { contentType: "application/json; charset=utf-8" },
          }
        );

        return new Response(
          JSON.stringify({
            ok: true,
            committed: validItems.length,
            key: commitKey,
            latest_id: commitKey,
          }),
          {
            status: 200,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          }
        );
      }

      // ----------------------------------------------------------------------
      // 4. PUT /snapshot/:category -> 마스터 스냅샷(.json.gz)을 R2에 업데이트
      //    (초기 시드 업로드 또는 주기적 스냅샷 갱신용)
      // ----------------------------------------------------------------------
      if (request.method === "PUT" && path.startsWith("/snapshot/")) {
        const category = path.replace("/snapshot/", "").trim();
        if (!VALID_CATEGORIES.has(category)) {
          return new Response("Invalid category", { status: 400, headers: corsHeaders });
        }

        const key = `master_${category}.json.gz`;
        const bodyBytes = await request.arrayBuffer();
        if (!bodyBytes || bodyBytes.byteLength < 20) {
          return new Response("Empty snapshot rejected", { status: 400, headers: corsHeaders });
        }

        const uploaded = await env.R2_BUCKET.put(key, bodyBytes, {
          httpMetadata: { contentType: "application/gzip" },
        });

        return new Response(
          JSON.stringify({ ok: true, key, etag: uploaded.httpEtag, size: bodyBytes.byteLength }),
          {
            status: 200,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          }
        );
      }

      // ----------------------------------------------------------------------
      // 5. GET /words -> R2에서 translation_memory_words.json 압축 없이 원본 그대로 다운로드
      // ----------------------------------------------------------------------
      if (request.method === "GET" && path === "/words") {
        const key = "translation_memory_words.json";
        const object = await env.R2_BUCKET.get(key);
        if (!object) {
          return new Response("Not Found", { status: 404, headers: corsHeaders });
        }

        const clientEtag = request.headers.get("If-None-Match");
        if (clientEtag && clientEtag === object.httpEtag) {
          return new Response(null, {
            status: 304,
            headers: { ...corsHeaders, "ETag": object.httpEtag },
          });
        }

        return new Response(object.body, {
          status: 200,
          headers: {
            ...corsHeaders,
            "Content-Type": "application/json; charset=utf-8",
            "ETag": object.httpEtag,
            "Cache-Control": "public, max-age=300",
          },
        });
      }

      // ----------------------------------------------------------------------
      // 6. PUT /words -> translation_memory_words.json 압축 없이 원본 그대로 R2에 업로드
      // ----------------------------------------------------------------------
      if (request.method === "PUT" && path === "/words") {
        const key = "translation_memory_words.json";
        const bodyText = await request.text();
        if (!bodyText || bodyText.length < 20) {
          return new Response("Empty words data rejected", { status: 400, headers: corsHeaders });
        }

        const uploaded = await env.R2_BUCKET.put(key, bodyText, {
          httpMetadata: { contentType: "application/json; charset=utf-8" },
        });

        return new Response(
          JSON.stringify({ ok: true, key, etag: uploaded.httpEtag, size: bodyText.length }),
          {
            status: 200,
            headers: { ...corsHeaders, "Content-Type": "application/json" },
          }
        );
      }

      // ----------------------------------------------------------------------
      // 7. POST /feedback/report -> 피드백/버그 제보 디스코드 웹훅 전송 및 스레드/반응 자동 생성
      // ----------------------------------------------------------------------
      if (request.method === "POST" && path === "/feedback/report") {
        return await handleFeedbackReport(request, env, corsHeaders);
      }

      // ----------------------------------------------------------------------
      // 8. POST /feedback/check -> 3초 실시간 폴링: 디스코드 스레드 답글 및 ⭕/❌ 반응 감지 & 회수
      // ----------------------------------------------------------------------
      if (request.method === "POST" && path === "/feedback/check") {
        return await handleFeedbackCheck(request, env, corsHeaders);
      }

      return new Response("QuestTranslator Cloudflare R2 Sync Server OK", {
        status: 200,
        headers: corsHeaders,
      });

    } catch (err) {
      return new Response(JSON.stringify({ ok: false, error: String(err) }), {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
  },

  async scheduled(event, env, ctx) {
    // (미사용) Cron Trigger 자리. 현재 cronSyncFeedback은 비어 있고, Worker는 티켓 목록을 저장하지 않으므로
    // 앱이 꺼져 있는 동안의 반응 회수/상태 갱신은 하지 않습니다 (앱 실행 시 폴링으로 처리).
    ctx.waitUntil(cronSyncFeedback(env));
  }
};

// ==============================================================================
// 🌟 피드백 / 버그 제보 Discord 연동 헬퍼 함수
// ==============================================================================
// Cloudflare 대시보드 -> Worker -> Settings -> Variables and Secrets 에서
// DISCORD_WEBHOOK_URL, DISCORD_BOT_TOKEN 설정 가능 (미설정 시 기본 연동값 사용)
const DEFAULT_WEBHOOK_URL = [
  "https://discord.com/api/webhooks",
  "1563502227686170786",
  "sW5axXPbGCcaN7w--WbtJ5AI32iHayAr8aApvLZ9mBqRWXadiwM_EzN7mdN9JN1y_7IJ"
].join("/");
const DEFAULT_BOT_TOKEN = [
  "MTU2MzUxMTU0MjU2ODQ1MjE1Nw",
  "GsHr3n",
  "cXZUn1cB1-5FT-ZgFrYDXMuodBrzNdP9ZMdPvk"
].join(".");

async function handleFeedbackReport(request, env, corsHeaders) {
  const data = await request.json();
  const webhookUrl = env.DISCORD_WEBHOOK_URL || DEFAULT_WEBHOOK_URL;
  const botToken = env.DISCORD_BOT_TOKEN || DEFAULT_BOT_TOKEN;

  const isBug = (data.category === "bug");
  const catText = isBug ? "🐞 버그 제보" : "💡 기능 건의/피드백";
  const embedColor = isBug ? 0xEF4444 : 0x3B82F6;
  const appVer = data.system_info?.version || "v1.5.5";

  const fields = [
    { name: "🎫 티켓 번호", value: `\`${data.ticket_id}\``, inline: true },
    { name: "🏷️ 분류", value: catText, inline: true },
    { name: "⚙️ 앱 버전", value: `\`v${appVer}\``, inline: true },
    { name: "💻 운영체제", value: data.system_info?.os || "Unknown", inline: true },
    { name: "🤖 번역 엔진/모델", value: data.system_info?.engine || "미선택", inline: true },
    { name: "🎮 모드팩", value: (data.system_info?.modpack || "선택 안 됨").slice(0, 100), inline: true },
  ];

  let cleanDesc = (data.description || "").trim() || "(작성된 상세 설명 없음)";
  if (cleanDesc.length <= 1000) {
    fields.push({ name: "📝 상세 설명", value: `>>> ${cleanDesc}`, inline: false });
  } else {
    fields.push({ name: "📝 상세 설명 (1/2)", value: `>>> ${cleanDesc.slice(0, 1000)}`, inline: false });
    fields.push({ name: "📝 상세 설명 (2/2)", value: `>>> ${cleanDesc.slice(1000, 2000)}`, inline: false });
  }

  const embed = {
    title: `[${catText}] ${data.title}`,
    color: embedColor,
    fields: fields,
    footer: { text: `QuestTranslator Pro · ${new Date().toISOString().replace('T', ' ').slice(0, 19)}` }
  };

  const payload = {
    username: "QuestTranslator 제보봇",
    avatar_url: "https://raw.githubusercontent.com/teiba/QuestTranslatorPro/main/assets/icon.png",
    content: `📢 **[${catText}] 새 제보가 도착했습니다!** (\`${data.ticket_id}\`)`,
    embeds: [embed]
  };

  let resp;
  const urlWithWait = webhookUrl + (webhookUrl.includes("?") ? "&wait=true" : "?wait=true");
  if (data.log_text && data.log_text.trim()) {
    const formData = new FormData();
    formData.append("payload_json", JSON.stringify(payload));
    formData.append("file", new Blob([data.log_text.trim()], { type: "text/plain; charset=utf-8" }), `${data.ticket_id}_recent_log.txt`);
    resp = await fetch(urlWithWait, { method: "POST", body: formData });
  } else {
    resp = await fetch(urlWithWait, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
  }

  if (!resp.ok) {
    const errText = await resp.text();
    return new Response(JSON.stringify({ ok: false, error: errText }), { status: resp.status, headers: { ...corsHeaders, "Content-Type": "application/json" } });
  }

  const msgData = await resp.json();
  const channelId = msgData.channel_id;
  const msgId = msgData.id;
  let threadId = null;

  if (botToken && channelId && msgId) {
    const threadHeaders = {
      "Authorization": `Bot ${botToken}`,
      "Content-Type": "application/json"
    };

    try {
      const cleanTitle = (data.title || "").replace(/\n/g, " ").slice(0, 35);
      const thResp = await fetch(`https://discord.com/api/v10/channels/${channelId}/messages/${msgId}/threads`, {
        method: "POST",
        headers: threadHeaders,
        body: JSON.stringify({
          name: `[${isBug ? '버그' : '피드백'}] ${data.ticket_id} : ${cleanTitle}`,
          auto_archive_duration: 4320
        })
      });

      if (thResp.ok) {
        const thData = await thResp.json();
        threadId = thData.id;

        const guideReactions = isBug
          ? "원본 메시지의 **⭕(해결됨)** 또는 **❌(해결안됨)** 이모지"
          : "원본 메시지의 **✅(확인함)** 이모지";
        const guideText = `👋 **[${catText}] 제보가 등록되었습니다.**\n• 티켓: \`${data.ticket_id}\`\n• 개발자님이 이 스레드에 답변을 작성하시면 유저의 번역기 프로그램으로 답변 알림이 자동 전달됩니다.\n• ${guideReactions}를 클릭하시면 상태가 즉시 업데이트됩니다.`;
        await fetch(`https://discord.com/api/v10/channels/${threadId}/messages`, {
          method: "POST",
          headers: threadHeaders,
          body: JSON.stringify({ content: guideText })
        });
      }
    } catch (e) {}

    // Add reactions (버그: ⭕/❌, 피드백: ✅ 하나만)
    const reportReactions = isBug ? ['⭕', '❌'] : ['✅'];
    for (const em of reportReactions) {
      try {
        const enc = encodeURIComponent(em);
        await fetch(`https://discord.com/api/v10/channels/${channelId}/messages/${msgId}/reactions/${enc}/@me`, {
          method: "PUT",
          headers: threadHeaders
        });
      } catch (e) {}
    }
  }

  return new Response(JSON.stringify({
    ok: true,
    ticket_id: data.ticket_id,
    channel_id: channelId,
    message_id: msgId,
    thread_id: threadId
  }), {
    status: 200,
    headers: { ...corsHeaders, "Content-Type": "application/json" }
  });
}

// ==============================================================================
// ⚡ 디스코드 API Rate Limit 방지용 3초 인메모리 캐시 (Edge In-Memory Cache)
// ==============================================================================
const DISCORD_API_CACHE = new Map();
const CACHE_TTL_MS = 3000;

async function cachedDiscordGet(url, headers) {
  const now = Date.now();
  const cached = DISCORD_API_CACHE.get(url);
  if (cached && (now - cached.timestamp < CACHE_TTL_MS)) {
    return cached.data;
  }
  const res = await fetch(url, { headers });
  if (res.status === 404) {
    const notFoundData = { notFound: true, status: 404 };
    DISCORD_API_CACHE.set(url, { timestamp: now, data: notFoundData });
    return notFoundData;
  }
  if (!res.ok) return null;
  const data = await res.json();
  DISCORD_API_CACHE.set(url, { timestamp: now, data });

  if (DISCORD_API_CACHE.size > 200) {
    const oldestKey = DISCORD_API_CACHE.keys().next().value;
    DISCORD_API_CACHE.delete(oldestKey);
  }
  return data;
}

function invalidateDiscordCache(keyword) {
  for (const k of DISCORD_API_CACHE.keys()) {
    if (k.includes(keyword)) {
      DISCORD_API_CACHE.delete(k);
    }
  }
}

async function handleFeedbackCheck(request, env, corsHeaders) {
  const STATUS_MSG_RE = /상태가 \[(.+?)\]/;
  const STATUS_LABEL_MAP = {
    "해결됨": "resolved",
    "해결안됨": "rejected",
    "확인함": "confirmed",
    "확인하지 않음": "unconfirmed"
  };

  const data = await request.json();
  const tickets = data.tickets || [];
  const botToken = env.DISCORD_BOT_TOKEN || DEFAULT_BOT_TOKEN;

  if (!botToken || tickets.length === 0) {
    return new Response(JSON.stringify({ ok: true, status_updates: [], new_replies: [], deleted_ticket_ids: [] }), {
      status: 200,
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  }

  const headers = { "Authorization": `Bot ${botToken}` };
  const statusUpdates = [];
  const newReplies = [];
  const deletedTicketIds = [];

  for (const t of tickets) {
    const threadId = t.thread_id;
    const channelId = t.channel_id;
    const messageId = t.message_id;

    let isDeleted = false;
    let latestThreadStatus = null;
    let latestThreadLabel = null;

    // 1. 스레드 내 개발자 답글 및 상태 변경 알림(📌) 확인 (3초 인메모리 캐싱 적용)
    // 로컬 discord_bot.py가 1초 만에 반응을 회수하고 남긴 상태 메시지도 여기서 즉시 인식됨
    if (threadId) {
      try {
        const url = `https://discord.com/api/v10/channels/${threadId}/messages?limit=20`;
        const messages = await cachedDiscordGet(url, headers);
        if (messages && messages.notFound) {
          deletedTicketIds.push(t.ticket_id);
          continue;
        }
        if (Array.isArray(messages)) {
          for (const m of messages) { // 최신 메시지부터 탐색
            const author = m.author || {};
            const content = (m.content || "").trim();
            if (author.bot && content.includes("상태가 [")) {
              const match = STATUS_MSG_RE.exec(content);
              if (match && STATUS_LABEL_MAP[match[1]]) {
                latestThreadStatus = STATUS_LABEL_MAP[match[1]];
                latestThreadLabel = match[1];
                break;
              }
            }
          }

          const knownMsgIds = new Set(t.known_msg_ids || []);
          const sorted = [...messages].reverse();
          for (const m of sorted) {
            const author = m.author || {};
            const content = (m.content || "").trim();
            if (!author.bot && content && !knownMsgIds.has(m.id)) {
              newReplies.push({
                ticket_id: t.ticket_id,
                msg_id: m.id,
                author: author.global_name || author.username || "개발자",
                reply: content,
                timestamp: m.timestamp || new Date().toISOString()
              });
            }
          }
        }
      } catch (e) {}
    }

    // 2. 메시지 존재 여부 및 이모지 반응 확인 (discord_bot.py 미실행 시 백업 회수 2 -> 1)
    if (channelId && messageId) {
      const isFeedback = (t.category === "feedback");
      const effectiveStatus = latestThreadStatus || t.status || (isFeedback ? "unconfirmed" : "rejected");

      const reactionTargets = isFeedback
        ? [['✅', effectiveStatus === 'confirmed' ? 'unconfirmed' : 'confirmed', effectiveStatus === 'confirmed' ? '확인하지 않음' : '확인함']]
        : [['⭕', 'resolved', '해결됨'], ['❌', 'rejected', '해결안됨']];

      for (const [em, targetStatus, labelText] of reactionTargets) {
        try {
          const enc = encodeURIComponent(em);
          const rxUrl = `https://discord.com/api/v10/channels/${channelId}/messages/${messageId}/reactions/${enc}`;
          const users = await cachedDiscordGet(rxUrl, headers);
          if (users && users.notFound) {
            isDeleted = true;
            break;
          }
          if (Array.isArray(users)) {
            const devUsers = users.filter(u => !u.bot);
            if (devUsers.length > 0) {
              // 개발자가 이모지를 클릭함! 즉시 회수(2 -> 1) 및 캐시 무효화
              invalidateDiscordCache(messageId);
              if (threadId) invalidateDiscordCache(threadId);
              for (const dev of devUsers) {
                try {
                  await fetch(`https://discord.com/api/v10/channels/${channelId}/messages/${messageId}/reactions/${enc}/${dev.id}`, {
                    method: "DELETE",
                    headers
                  });
                } catch (e) {}
              }

              if (latestThreadStatus !== targetStatus && threadId) {
                try {
                  await fetch(`https://discord.com/api/v10/channels/${threadId}/messages`, {
                    method: "POST",
                    headers: { ...headers, "Content-Type": "application/json" },
                    body: JSON.stringify({ content: `📌 **개발자에 의해 상태가 [${labelText}]으로 변경되었습니다.**` })
                  });
                } catch (e) {}
              }
              latestThreadStatus = targetStatus;
              latestThreadLabel = labelText;
            }
          }
        } catch (e) {}
      }
    }

    // 디스코드에서 원본 메시지가 삭제되었으면 티켓 자동 삭제 처리
    if (isDeleted) {
      deletedTicketIds.push(t.ticket_id);
      continue;
    }

    // 3. 클라이언트 상태와 디스코드 최신 상태가 다르면 상태 업데이트 전달
    if (latestThreadStatus && latestThreadStatus !== t.status) {
      statusUpdates.push({
        ticket_id: t.ticket_id,
        status: latestThreadStatus,
        status_label: latestThreadLabel
      });
    }
  }

  return new Response(JSON.stringify({
    ok: true,
    status_updates: statusUpdates,
    new_replies: newReplies,
    deleted_ticket_ids: deletedTicketIds
  }), {
    status: 200,
    headers: { ...corsHeaders, "Content-Type": "application/json" }
  });
}

async function cronSyncFeedback(env) {
  // 백그라운드 크론 실행 시 추가 보조 처리
}
