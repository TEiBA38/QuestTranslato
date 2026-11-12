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

          const snapCommitId = object.customMetadata?.last_commit_id || "";
          const clientEtag = request.headers.get("If-None-Match");
          if (clientEtag && clientEtag === object.httpEtag) {
            return new Response(null, {
              status: 304,
              headers: {
                ...corsHeaders,
                "ETag": object.httpEtag,
                "X-Last-Commit-Id": snapCommitId,
              },
            });
          }

          return new Response(object.body, {
            status: 200,
            headers: {
              ...corsHeaders,
              "Content-Type": "application/gzip",
              "ETag": object.httpEtag,
              "X-Last-Commit-Id": snapCommitId,
              "Cache-Control": "no-cache",
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
        // 3-2. POST /commits/purge -> 병합된 과거 커밋 파일 일괄 정리 (용량 최적화)
        // ----------------------------------------------------------------------
        if (request.method === "POST" && path === "/commits/purge") {
          let purgedCount = 0;
          const purgeUntil = (url.searchParams.get("until") || "").trim();
          const purgeAll = url.searchParams.get("all") === "true";

          let cursor = undefined;
          while (true) {
            const listOpt = { prefix: "commits/", limit: 1000, cursor };
            const listed = await env.R2_BUCKET.list(listOpt);
            if (!listed.objects || listed.objects.length === 0) break;

            const keysToDelete = [];
            for (const obj of listed.objects) {
              if (purgeAll || (purgeUntil && obj.key <= purgeUntil)) {
                keysToDelete.push(obj.key);
              }
            }

            if (keysToDelete.length > 0) {
              await env.R2_BUCKET.delete(keysToDelete);
              purgedCount += keysToDelete.length;
            }

            if (!listed.truncated || (purgeUntil && listed.objects[listed.objects.length - 1].key > purgeUntil)) {
              break;
            }
            cursor = listed.cursor;
          }

          return new Response(JSON.stringify({ ok: true, purged: purgedCount }), {
            status: 200,
            headers: { ...corsHeaders, "Content-Type": "application/json" }
          });
        }

        // ----------------------------------------------------------------------
        // 4. PUT /snapshot/:category -> 마스터 스냅샷(.json.gz)을 R2에 업데이트
        //    (초기 시드 업로드 또는 주기적 스냅샷 갱신 & 이전 커밋 자동 정리 지원)
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

          const purgeUntil = (url.searchParams.get("purge_until") || "").trim();
          const commitId = (url.searchParams.get("commit_id") || purgeUntil).trim();
          const purgeAll = url.searchParams.get("purge_all") === "true";

          // 🌟 1~2초 동시성 충돌 방어 (409 Conflict): 서버에 이미 더 최신 커밋을 포함한 마스터가 있다면 거부
          if (commitId && !purgeAll) {
            try {
              const existing = await env.R2_BUCKET.head(key);
              const serverLastCommit = existing?.customMetadata?.last_commit_id || "";
              if (serverLastCommit && commitId <= serverLastCommit) {
                return new Response(
                  JSON.stringify({
                    ok: false,
                    error: "Conflict: Server master already contains newer or equal commits.",
                    server_last_commit: serverLastCommit,
                  }),
                  { status: 409, headers: { ...corsHeaders, "Content-Type": "application/json" } }
                );
              }
            } catch (headErr) {
              // 최초 생성 등 예외는 무시하고 계속 진행
            }
          }

          const putOptions = {
            httpMetadata: { contentType: "application/gzip" },
          };
          if (commitId) {
            putOptions.customMetadata = { last_commit_id: commitId };
          }

          const uploaded = await env.R2_BUCKET.put(key, bodyBytes, putOptions);

          // 🌟 스냅샷에 통합 완료된 과거 커밋 파일 자동 정리 (purge)
          let purgedCount = 0;

          if (purgeUntil || purgeAll) {
            let cursor = undefined;
            while (true) {
              const listOpt = { prefix: "commits/", limit: 1000, cursor };
              const listed = await env.R2_BUCKET.list(listOpt);
              if (!listed.objects || listed.objects.length === 0) break;

              const keysToDelete = [];
              for (const obj of listed.objects) {
                if (purgeAll || (purgeUntil && obj.key <= purgeUntil)) {
                  keysToDelete.push(obj.key);
                }
              }

              if (keysToDelete.length > 0) {
                await env.R2_BUCKET.delete(keysToDelete);
                purgedCount += keysToDelete.length;
              }

              if (!listed.truncated || (purgeUntil && listed.objects[listed.objects.length - 1].key > purgeUntil)) {
                break;
              }
              cursor = listed.cursor;
            }
          }

          return new Response(
            JSON.stringify({
              ok: true,
              key,
              etag: uploaded.httpEtag,
              size: bodyBytes.byteLength,
              purged_commits: purgedCount
            }),
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
        // 6-2. GET /ko-pack-keys -> R2에서 ko_pack_keys.json.gz (공식 보호 키) 다운로드
        // ----------------------------------------------------------------------
        if (request.method === "GET" && path === "/ko-pack-keys") {
          const key = "ko_pack_keys.json.gz";
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
              "Content-Type": "application/json",
              "Content-Encoding": "gzip",
              "ETag": object.httpEtag,
              "Cache-Control": "public, max-age=300",
            },
          });
        }

        // ----------------------------------------------------------------------
        // 6-3. PUT /ko-pack-keys -> ko_pack_keys.json.gz R2에 압축 업로드
        // ----------------------------------------------------------------------
        if (request.method === "PUT" && path === "/ko-pack-keys") {
          const key = "ko_pack_keys.json.gz";
          const bodyBuffer = await request.arrayBuffer();
          if (!bodyBuffer || bodyBuffer.byteLength < 10) {
            return new Response("Empty ko-pack-keys data rejected", { status: 400, headers: corsHeaders });
          }

          const uploaded = await env.R2_BUCKET.put(key, bodyBuffer, {
            httpMetadata: { contentType: "application/json", contentEncoding: "gzip" },
          });

          return new Response(
            JSON.stringify({ ok: true, key, etag: uploaded.httpEtag, size: bodyBuffer.byteLength }),
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
        // 8. POST /feedback/check -> 큐 기반 실시간 확인: 상태 갱신 및 새 답변 수신
        // ----------------------------------------------------------------------
        if (request.method === "POST" && path === "/feedback/check") {
          return await handleFeedbackCheck(request, env, corsHeaders);
        }

        // ----------------------------------------------------------------------
        // 8-2. POST /feedback/reply -> 유저 앱에서 디스코드 스레드로 답장 전송
        // ----------------------------------------------------------------------
        if (request.method === "POST" && path === "/feedback/reply") {
          return await handleUserReply(request, env, corsHeaders);
        }

        // ----------------------------------------------------------------------
        // 8-3. POST /feedback/retract -> 유저의 제보 철회 (디스코드 메시지/스레드 및 큐 동시 삭제)
        // ----------------------------------------------------------------------
        if (request.method === "POST" && path === "/feedback/retract") {
          return await handleFeedbackRetract(request, env, corsHeaders);
        }

        // ----------------------------------------------------------------------
        // 9. POST /audit/report -> 오역 수정 감사 로그를 디스코드 웹훅으로 전송 및 [⏪ 롤백] 버튼 부착
        // ----------------------------------------------------------------------
        if (request.method === "POST" && path === "/audit/report") {
          return await handleAuditReport(request, env, corsHeaders);
        }

        if (request.method === "GET" && path === "/audit/status") {
          return await handleAuditStatus(request, env, corsHeaders);
        }

        // ----------------------------------------------------------------------
        // 9-2. GET /audit/rollback?audit_id=<id> -> 원클릭 링크 롤백
        // ----------------------------------------------------------------------
        if (request.method === "GET" && path === "/audit/rollback") {
          return await handleAuditLinkRollback(request, env, corsHeaders);
        }

        // ----------------------------------------------------------------------
        // 10. POST /discord/interactions -> 디스코드 버튼 딸깍 클릭(Interaction) 즉시 수신 및 원클릭 롤백
        // ----------------------------------------------------------------------
        const normPath = path.replace(/\/+$/, "");
        if (request.method === "POST" && (normPath === "/discord/interactions" || normPath === "/interactions")) {
          return await handleDiscordInteractions(request, env, corsHeaders);
        }

        // ----------------------------------------------------------------------
        // 10-1. GET /discord/setup-commands -> 디스코드 슬래시 명령어(/답변, /reply) 원클릭 자동 등록
        // ----------------------------------------------------------------------
        if (request.method === "GET" && (normPath === "/discord/setup-commands" || normPath === "/setup-commands")) {
          return await handleSetupDiscordCommands(request, env, corsHeaders);
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
  const DEFAULT_CHANNEL_ID = "1563501678127358094";
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

  function getKoreanTimeString() {
    const now = new Date();
    const kst = new Date(now.getTime() + 9 * 60 * 60 * 1000);
    return kst.toISOString().replace('T', ' ').slice(0, 19);
  }

  async function handleFeedbackReport(request, env, corsHeaders) {
    const data = await request.json();
    const webhookUrl = env.DISCORD_WEBHOOK_URL || DEFAULT_WEBHOOK_URL;
    const botToken = env.DISCORD_BOT_TOKEN || DEFAULT_BOT_TOKEN;
    const channelId = env.DISCORD_CHANNEL_ID || DEFAULT_CHANNEL_ID;

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
      footer: { text: `QuestTranslator Pro · ${getKoreanTimeString()} (KST)` }
    };

    const components = isBug ? [
      {
        type: 1,
        components: [
          { type: 2, style: 1, label: "✅ 확인됨", custom_id: `fb_st:confirmed:${data.ticket_id}` },
          { type: 2, style: 3, label: "⭕ 해결됨", custom_id: `fb_st:resolved:${data.ticket_id}` }
        ]
      }
    ] : [
      {
        type: 1,
        components: [
          { type: 2, style: 1, label: "✅ 확인됨", custom_id: `fb_st:confirmed:${data.ticket_id}` },
          { type: 2, style: 3, label: "🚀 반영됨", custom_id: `fb_st:implemented:${data.ticket_id}` },
          { type: 2, style: 4, label: "🚫 반려됨", custom_id: `fb_st:rejected:${data.ticket_id}` }
        ]
      }
    ];

    const payload = {
      content: `📢 **[${catText}] 새 제보가 도착했습니다!** (\`${data.ticket_id}\`)`,
      embeds: [embed],
      components: components
    };

    let resp;
    let targetChannelId = channelId;

    if (botToken && channelId) {
      // 🌟 봇 토큰으로 채널에 직접 메시지 전송 -> 버튼(components)이 100% 정상 표시됨!
      const botHeaders = {
        "Authorization": `Bot ${botToken}`
      };

      if (data.log_text && data.log_text.trim()) {
        const formData = new FormData();
        formData.append("payload_json", JSON.stringify(payload));
        formData.append("files[0]", new Blob([data.log_text.trim()], { type: "text/plain; charset=utf-8" }), `${data.ticket_id}_recent_log.txt`);
        resp = await fetch(`https://discord.com/api/v10/channels/${channelId}/messages`, {
          method: "POST",
          headers: botHeaders,
          body: formData
        });
      } else {
        resp = await fetch(`https://discord.com/api/v10/channels/${channelId}/messages`, {
          method: "POST",
          headers: {
            ...botHeaders,
            "Content-Type": "application/json"
          },
          body: JSON.stringify(payload)
        });
      }
    } else {
      // 봇 토큰이 없을 경우 웹훅으로 전송 (웹훅은 버튼 미지원)
      const urlWithWait = webhookUrl + (webhookUrl.includes("?") ? "&wait=true" : "?wait=true");
      const webhookPayload = {
        username: "QuestTranslator 제보봇",
        avatar_url: "https://raw.githubusercontent.com/teiba/QuestTranslatorPro/main/assets/icon.png",
        content: payload.content,
        embeds: payload.embeds
      };

      if (data.log_text && data.log_text.trim()) {
        const formData = new FormData();
        formData.append("payload_json", JSON.stringify(webhookPayload));
        formData.append("file", new Blob([data.log_text.trim()], { type: "text/plain; charset=utf-8" }), `${data.ticket_id}_recent_log.txt`);
        resp = await fetch(urlWithWait, { method: "POST", body: formData });
      } else {
        resp = await fetch(urlWithWait, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(webhookPayload)
        });
      }
    }

    if (!resp.ok) {
      const errText = await resp.text();
      return new Response(JSON.stringify({ ok: false, error: errText }), { status: resp.status, headers: { ...corsHeaders, "Content-Type": "application/json" } });
    }

    const msgData = await resp.json();
    targetChannelId = msgData.channel_id || channelId;
    const msgId = msgData.id;
    let threadId = null;

    if (botToken && targetChannelId && msgId) {
      const threadHeaders = {
        "Authorization": `Bot ${botToken}`,
        "Content-Type": "application/json"
      };

      try {
        const cleanTitle = (data.title || "").replace(/\n/g, " ").slice(0, 35);
        const thResp = await fetch(`https://discord.com/api/v10/channels/${targetChannelId}/messages/${msgId}/threads`, {
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

          // 스레드 ID <-> 티켓 매핑 저장 (슬래시 명령어 /답변 시 즉시 역조회)
          if (env.R2_BUCKET) {
            await env.R2_BUCKET.put(`feedback/thread_map/${threadId}.json`, JSON.stringify({
              ticket_id: data.ticket_id,
              category: data.category,
              channel_id: targetChannelId,
              message_id: msgId,
              thread_id: threadId
            }), { httpMetadata: { contentType: "application/json" } });

            // 초기 큐 생성 (기본 상태: 🟡 확인중)
            await env.R2_BUCKET.put(`feedback/queue/${data.ticket_id}.json`, JSON.stringify({
              ticket_id: data.ticket_id,
              category: data.category,
              thread_id: threadId,
              channel_id: targetChannelId,
              message_id: msgId,
              status: "reviewing",
              status_label: "확인중",
              pending_replies: [],
              status_history: [{
                status: "reviewing",
                label: "확인중",
                timestamp: new Date().toISOString()
              }]
            }), { httpMetadata: { contentType: "application/json" } });
          }

          const guideText = `👋 **[${catText}] 제보가 등록되었습니다.**\n• 티켓: \`${data.ticket_id}\`\n• 📌 **상태 변경**: 상단 원본 메시지의 버튼을 탭하면 즉시 반영됩니다.\n• 💬 **답변 작성**: 이 스레드에서 \`/답변 [내용]\` 명령어를 사용하시면 유저 앱으로 실시간 전달됩니다.`;
          await fetch(`https://discord.com/api/v10/channels/${threadId}/messages`, {
            method: "POST",
            headers: threadHeaders,
            body: JSON.stringify({ content: guideText })
          });
        }
      } catch (e) {}
    }

    return new Response(JSON.stringify({
      ok: true,
      ticket_id: data.ticket_id,
      channel_id: targetChannelId,
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

      // 🚀 0. 워커 R2 큐 우선 확인 (디스코드 REST API 호출 0회, Rate Limit 100% 회피 및 즉각 영수증)
      let queueHandled = false;
      if (env.R2_BUCKET) {
        try {
          const qFile = await env.R2_BUCKET.get(`feedback/queue/${t.ticket_id}.json`);
          if (qFile) {
            const queueData = await qFile.json();
            queueHandled = true;

            // 상태 변경 확인
            if (queueData.status && queueData.status !== t.status) {
              statusUpdates.push({
                ticket_id: t.ticket_id,
                status: queueData.status,
                status_label: queueData.status_label || queueData.status,
                status_history: queueData.status_history || []
              });
            }

            // 대기 중인 개발자 답변 확인 및 유저 수신 영수증 발행
            let newDelivered = 0;
            const pendingReplies = queueData.pending_replies || [];
            const knownMsgIds = new Set(t.known_msg_ids || []);

            for (const r of pendingReplies) {
              if (!knownMsgIds.has(r.msg_id)) {
                newReplies.push({
                  ticket_id: t.ticket_id,
                  msg_id: r.msg_id,
                  author: r.author || "개발자",
                  reply: r.content,
                  timestamp: r.timestamp
                });
                if (!r.delivered) {
                  r.delivered = true;
                  newDelivered++;
                }
              }
            }

            // 유저 앱으로 정상 배달되었으면 스레드에 [수신 완료] 영수증 로그 게시 & R2 갱신
            if (newDelivered > 0) {
              await env.R2_BUCKET.put(`feedback/queue/${t.ticket_id}.json`, JSON.stringify(queueData), {
                httpMetadata: { contentType: "application/json" }
              });

              const targetThId = queueData.thread_id || threadId;
              if (botToken && targetThId) {
                try {
                  const nowStr = new Date().toISOString().replace('T', ' ').slice(0, 19);
                  await fetch(`https://discord.com/api/v10/channels/${targetThId}/messages`, {
                    method: "POST",
                    headers: { "Authorization": `Bot ${botToken}`, "Content-Type": "application/json" },
                    body: JSON.stringify({
                      content: `✅ **[유저 수신 완료]** 유저가 번역기 앱에서 답변(${newDelivered}건)을 정상 확인했습니다! (${nowStr} UTC)`
                    })
                  });
                } catch (_) {}
              }
            }
          }
        } catch (_) {}
      }
      if (queueHandled) continue;

      // 1. (레거시 폴백) 스레드 내 개발자 답글 및 상태 변경 알림(📌) 확인 (3초 인메모리 캐싱 적용)
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
                  author: "개발자",
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


  // ==============================================================================
  // 🛡️ 오역 수정 감사 로그 & 디스코드 버튼 즉시 롤백 (Interactions)
  // ==============================================================================
  // Cloudflare 대시보드 -> Worker -> Settings -> Variables and Secrets 에서
  // DISCORD_AUDIT_WEBHOOK_URL, DISCORD_PUBLIC_KEY, DISCORD_BOT_TOKEN 설정 (비밀변수 권장)
  const DEFAULT_AUDIT_WEBHOOK_URL = [
    "https://discordapp.com/api/webhooks",
    "1567785312665608213",
    "_sBZuIeTus_bIacmss1BEKH6G8ToU1giBn4l-KdghM8cg8vd3ZcRyG-fDBKNwA80wg9z"
  ].join("/");

  async function handleAuditStatus(request, env, corsHeaders) {
  const url = new URL(request.url);
  const auditId = (url.searchParams.get("audit_id") || "").trim();
  if (!auditId || !env.R2_BUCKET) {
    return new Response(JSON.stringify({ ok: false, error: "Missing audit_id or R2 bucket" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  }

  const file = await env.R2_BUCKET.get(`audit/${auditId}.json`);
  if (!file) {
    return new Response(JSON.stringify({ ok: false, not_found: true }), {
      status: 404,
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  }

  const data = await file.json();
  return new Response(JSON.stringify({
    ok: true,
    audit_id: auditId,
    is_test: !!data.is_test,
    rolled_back: !!data.rolled_back,
    rolled_back_by: data.rolled_back_by || "",
    rolled_back_at: data.rolled_back_at || 0,
    old_tgt: data.old_tgt || "",
    new_tgt: data.new_tgt || "",
    src: data.src || ""
  }), {
    status: 200,
    headers: { ...corsHeaders, "Content-Type": "application/json" }
  });
}

async function handleAuditLinkRollback(request, env, corsHeaders) {
  const url = new URL(request.url);
  const auditId = (url.searchParams.get("audit_id") || "").trim();
  if (!auditId || !env.R2_BUCKET) {
    return new Response("유효하지 않은 요청입니다.", { status: 400 });
  }

  const file = await env.R2_BUCKET.get(`audit/${auditId}.json`);
  if (!file) {
    return new Response("해당 감사 기록을 찾을 수 없습니다.", { status: 404 });
  }

  const auditData = await file.json();
  const isTest = !!auditData.is_test;

  if (auditData.rolled_back) {
    return new Response(`
      <!DOCTYPE html>
      <html>
      <head><meta charset="utf-8"><title>롤백 완료</title></head>
      <body style="font-family: sans-serif; background: #18181b; color: #f4f4f5; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0;">
        <div style="background: #27272a; padding: 32px; border-radius: 12px; text-align: center; max-width: 480px; box-shadow: 0 4px 20px rgba(0,0,0,0.5);">
          <h2 style="color: #fbbf24; margin-top: 0;">⚠️ 이미 롤백 완료된 항목입니다</h2>
          <p style="color: #a1a1aa;">이 항목은 이미 이전 번역으로 복구되었습니다.</p>
        </div>
      </body>
      </html>
    `, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
  }

  const now = Date.now();
  if (!isTest) {
    const commitId = `${now.toString().padStart(16, "0")}_${crypto.randomUUID()}`;
    const commitKey = `commits/${commitId}.json`;
    const rollbackItem = {
      cat: auditData.category || "items",
      lang: auditData.lang || "한국어 (Korean)",
      src: auditData.src,
      tgt: auditData.old_tgt
    };
    await env.R2_BUCKET.put(commitKey, JSON.stringify({
      timestamp: now,
      items: [rollbackItem]
    }), {
      httpMetadata: { contentType: "application/json; charset=utf-8" }
    });
  }

  auditData.rolled_back = true;
  auditData.rolled_back_at = now;
  auditData.rolled_back_by = "디스코드 관리자 (원클릭 링크)";
  await env.R2_BUCKET.put(`audit/${auditId}.json`, JSON.stringify(auditData), {
    httpMetadata: { contentType: "application/json; charset=utf-8" }
  });

  // 🔔 디스코드 원본 임베드 메시지를 [롤백 완료] 상태로 실시간 업데이트
  const webhookUrl = env.DISCORD_AUDIT_WEBHOOK_URL || DEFAULT_AUDIT_WEBHOOK_URL;
  if (auditData.discord_message_id && webhookUrl) {
    try {
      const cleanWebhook = webhookUrl.split("?")[0];
      const patchUrl = `${cleanWebhook}/messages/${auditData.discord_message_id}`;
      const rolledEmbed = {
        title: isTest ? "🧪 [테스트] 롤백 완료 (번역 복구됨)" : "✅ [롤백 완료] 이전 번역으로 복구됨",
        color: 0x10B981, // 산뜻한 에메랄드 초록색
        description: `🎉 **이 번역 수정 내역은 관리자에 의해 원래 값으로 정상 롤백되었습니다.**\n(완료 일시: ${new Date(now).toISOString().replace('T', ' ').slice(0, 19)} UTC)`,
        fields: [
          { name: "📌 원문 (src)", value: `\`${auditData.src}\``, inline: false },
          { name: "❌ 취소된 번역 (수정 취소)", value: `~~${auditData.new_tgt}~~`, inline: true },
          { name: "✅ 복구된 번역 (원복 완료)", value: `**${auditData.old_tgt || "(삭제)"}**`, inline: true },
          { name: "상태", value: "`🟢 롤백 완료됨 (원복)`", inline: true },
          { name: "🏷️ 카테고리", value: `\`${auditData.category}\`${isTest ? " (🧪 테스트)" : ""}`, inline: true }
        ],
        footer: { text: "QuestTranslator Pro · 롤백 완료됨" }
      };

      await fetch(patchUrl, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: `⏪ **[롤백 완료]** \`${auditData.src}\` 항목이 이전 번역(\`${auditData.old_tgt || "(삭제)"}\`)(으)로 복구되었습니다!`,
          embeds: [rolledEmbed],
          components: []
        })
      });
    } catch (e) {
      console.warn("[Rollback Webhook Patch Failed]", e);
    }
  }

  return new Response(`
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>롤백 성공</title></head>
    <body style="font-family: sans-serif; background: #18181b; color: #f4f4f5; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0;">
      <div style="background: #27272a; padding: 32px; border-radius: 12px; text-align: center; max-width: 480px; box-shadow: 0 4px 20px rgba(0,0,0,0.5);">
        <h2 style="color: #34d399; margin-top: 0;">✅ 롤백이 성공적으로 완료되었습니다!</h2>
        <p style="font-size: 16px; margin: 16px 0;"><strong>${auditData.src}</strong></p>
        <p style="color: #a1a1aa;">수정 번역: <del style="color: #f87171;">${auditData.new_tgt}</del><br>복구 번역: <strong style="color: #34d399;">${auditData.old_tgt || "(삭제)"}</strong></p>
        <p style="color: #71717a; font-size: 13px; margin-top: 24px;">${isTest ? "🧪 [테스트 모드] 임시 파일이 복구되었습니다." : "클라우드 DB에 원복 커밋이 반영되었습니다."}</p>
      </div>
    </body>
    </html>
  `, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
}

async function handleAuditReport(request, env, corsHeaders) {
  try {
    const data = await request.json();
    const webhookUrl = env.DISCORD_AUDIT_WEBHOOK_URL || DEFAULT_AUDIT_WEBHOOK_URL;
    const auditId = `audit_${Date.now()}_${crypto.randomUUID().slice(0, 8)}`;
    const now = Date.now();
    const isTest = !!data.is_test;

    const auditRecord = {
      audit_id: auditId,
      timestamp: now,
      src: String(data.src || "").trim(),
      old_tgt: String(data.old_tgt || "").trim(),
      new_tgt: String(data.new_tgt || "").trim(),
      category: String(data.category || "items").trim(),
      lang: String(data.lang || "한국어 (Korean)").trim(),
      error_rate: Number(data.error_rate || 0),
      validity: Number(data.validity || 0),
      verdict: String(data.verdict || "pass"),
      details: String(data.details || ""),
      is_test: isTest,
      rolled_back: false
    };

    if (env.R2_BUCKET) {
      await env.R2_BUCKET.put(`audit/${auditId}.json`, JSON.stringify(auditRecord), {
        httpMetadata: { contentType: "application/json; charset=utf-8" },
      });
    }

    // 수정 시 임베드 색상은 항상 밝고 눈에 띄는 노란색(0xFEE75C)으로 통일
    const embedColor = isTest ? 0x8B5CF6 : 0xFEE75C;

    let titleText = isTest
      ? "🧪 [테스트 모드] 번역 수정 알림"
      : "📝 번역 수정 알림";

    let descText = isTest
      ? "테스트 모드(샌드박스)에서 임시 단어 수정이 발생했습니다.\n운영 파일은 보호되며, 아래 초록색 [롤백] 버튼으로 **롤백 시뮬레이션**을 검증할 수 있습니다."
      : "인게임 오역 수정기에서 번역 데이터가 수정되었습니다.\n원복이 필요하시면 아래 초록색 [롤백] 버튼을 클릭하세요.";

    const embed = {
      title: titleText,
      color: embedColor,
      description: descText,
      fields: [
        { name: "📌 영어 원문 (src)", value: `\`${auditRecord.src}\``, inline: false },
        { name: "⬅️ 이전 번역 (old)", value: `\`${auditRecord.old_tgt || (auditRecord.category && auditRecord.category.includes("일괄 치환") ? "일괄 치환" : "(없음)")}\``, inline: true },
        { name: "➡️ 수정 번역 (new)", value: `\`${auditRecord.new_tgt}\``, inline: true },
        { name: "📊 오역률 / 판정", value: `\`${auditRecord.error_rate.toFixed(1)}%\` (${auditRecord.verdict.toUpperCase()})`, inline: true },
        { name: "🏷️ 카테고리", value: `\`${auditRecord.category}\`${isTest ? " (🧪 테스트)" : ""}`, inline: true },
        { name: "🔍 분석 사유", value: auditRecord.details || "정상 수정", inline: false }
      ],
      footer: { text: `QuestTranslator Audit | ID: ${auditId}${isTest ? " | [TEST MODE]" : ""}` },
      timestamp: new Date().toISOString()
    };

    const components = [
      {
        type: 1,
        components: [
          {
            type: 2,
            style: 3, // 초록색 버튼 (Success)
            label: isTest ? "[테스트] 이전 번역으로 롤백" : "이전 번역으로 롤백",
            emoji: { name: "⏪" },
            custom_id: `rb:${auditId}`
          }
        ]
      }
    ];

    // 1순위: Discord Bot API 직접 호출 (인증된 Bot Token으로 버튼 컴포넌트 100% 정상 렌더링!)
    const botToken = env.DISCORD_BOT_TOKEN || DEFAULT_BOT_TOKEN;
    let targetChannelId = env.DISCORD_AUDIT_CHANNEL_ID;
    let postSucceeded = false;

    if (!targetChannelId && webhookUrl) {
      try {
        const whInfoRes = await fetch(webhookUrl);
        if (whInfoRes.ok) {
          const whInfo = await whInfoRes.json();
          targetChannelId = whInfo.channel_id;
        }
      } catch (_) {}
    }

    if (botToken && targetChannelId) {
      try {
        const botResp = await fetch(`https://discord.com/api/v10/channels/${targetChannelId}/messages`, {
          method: "POST",
          headers: {
            "Authorization": `Bot ${botToken}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            embeds: [embed],
            components: components
          })
        });
        if (botResp.ok) {
          const msgJson = await botResp.json();
          if (msgJson && msgJson.id) {
            auditRecord.discord_message_id = msgJson.id;
            postSucceeded = true;
            if (env.R2_BUCKET) {
              await env.R2_BUCKET.put(`audit/${auditId}.json`, JSON.stringify(auditRecord), {
                httpMetadata: { contentType: "application/json; charset=utf-8" },
              });
            }
          }
        } else {
          console.warn("[Audit Bot API Error]", await botResp.text());
        }
      } catch (botErr) {
        console.warn("[Audit Bot API Exception]", botErr);
      }
    }

    // 2순위 폴백: Bot API 실패 시 일반 웹훅으로 전송
    if (!postSucceeded) {
      let postResp = await fetch(`${webhookUrl}?wait=true`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          embeds: [embed],
          components: components
        })
      });

      if (!postResp.ok) {
        postResp = await fetch(`${webhookUrl}?wait=true`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            embeds: [embed]
          })
        });
      }

      if (postResp.ok) {
        try {
          const msgJson = await postResp.json();
          if (msgJson && msgJson.id) {
            auditRecord.discord_message_id = msgJson.id;
            if (env.R2_BUCKET) {
              await env.R2_BUCKET.put(`audit/${auditId}.json`, JSON.stringify(auditRecord), {
                httpMetadata: { contentType: "application/json; charset=utf-8" },
              });
            }
          }
        } catch (_) {}
      }
    }

    return new Response(JSON.stringify({ ok: true, audit_id: auditId, is_test: isTest }), {
      status: 200,
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  } catch (err) {
    return new Response(JSON.stringify({ ok: false, error: String(err) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  }
}

// ------------------------------------------------------------------------------
// 디스코드 인터랙션 (버튼 딸깍 클릭 즉시 수신 및 Ed25519 서명 검증)
// ------------------------------------------------------------------------------
async function handleDiscordInteractions(request, env, corsHeaders) {
  const { isValid, body } = await verifyDiscordSignature(request, env);
  if (!isValid) {
    return new Response("Invalid request signature", { status: 401 });
  }

  let interaction;
  try {
    interaction = JSON.parse(body);
  } catch (e) {
    return new Response("Bad Request", { status: 400 });
  }

  if (interaction.type === 1) {
    return new Response(JSON.stringify({ type: 1 }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    });
  }

  if (interaction.type === 3) {
    const customId = interaction.data?.custom_id || "";
    if (customId.startsWith("rb:")) {
      const auditId = customId.replace("rb:", "");
      if (!env.R2_BUCKET) {
        return new Response(JSON.stringify({
          type: 4,
          data: { content: "⚠️ R2 버킷 바인딩이 누락되었습니다.", flags: 64 }
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }

      const auditFile = await env.R2_BUCKET.get(`audit/${auditId}.json`);
      if (!auditFile) {
        return new Response(JSON.stringify({
          type: 4,
          data: { content: "⚠️ 롤백할 감사 기록을 찾을 수 없습니다.", flags: 64 }
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }

      const auditData = await auditFile.json();
      if (auditData.rolled_back) {
        return new Response(JSON.stringify({
          type: 4,
          data: { content: `⚠️ 이미 롤백 완료된 항목입니다. (롤백자: ${auditData.rolled_back_by || "관리자"})`, flags: 64 }
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }

      const now = Date.now();
      const isTest = !!auditData.is_test;

      if (!isTest) {
        const commitId = `${now.toString().padStart(16, "0")}_${crypto.randomUUID()}`;
        const commitKey = `commits/${commitId}.json`;
        const rollbackItem = {
          cat: auditData.category || "items",
          lang: auditData.lang || "한국어 (Korean)",
          src: auditData.src,
          tgt: auditData.old_tgt
        };

        await env.R2_BUCKET.put(commitKey, JSON.stringify({
          timestamp: now,
          items: [rollbackItem]
        }), {
          httpMetadata: { contentType: "application/json; charset=utf-8" }
        });
      }

      const adminName = interaction.member?.user?.username || interaction.user?.username || "관리자";
      const adminId = interaction.member?.user?.id || interaction.user?.id || "";
      auditData.rolled_back = true;
      auditData.rolled_back_at = now;
      auditData.rolled_back_by = adminName;
      await env.R2_BUCKET.put(`audit/${auditId}.json`, JSON.stringify(auditData), {
        httpMetadata: { contentType: "application/json; charset=utf-8" },
      });

      const origEmbed = (interaction.message?.embeds && interaction.message.embeds[0]) || {};
      const successNotice = isTest
        ? `\n\n✅ **[테스트 롤백 성공]** 관리자(${adminId ? `<@${adminId}>` : adminName})에 의해 테스트 롤백이 성공적으로 완료되었습니다!\n(운영 DB 미반영 🛡️ / 로컬 temp 복구 완료)`
        : `\n\n✅ **[원클릭 롤백 완료]** 관리자(${adminId ? `<@${adminId}>` : adminName})에 의해 원본 번역 \`${auditData.old_tgt || "(삭제)"}\`(으)로 클라우드 DB가 즉시 원복되었습니다.`;

      const updatedEmbed = {
        ...origEmbed,
        color: 0x10B981,
        description: (origEmbed.description || "") + successNotice
      };

      return new Response(JSON.stringify({
        type: 7,
        data: {
          embeds: [updatedEmbed],
          components: [
            {
              type: 1,
              components: [
                {
                  type: 2,
                  style: 2,
                  label: isTest ? "[테스트] 롤백 완료됨" : "롤백 완료됨",
                  emoji: { name: "✅" },
                  custom_id: `rb_done:${auditId}`,
                  disabled: true
                }
              ]
            }
          ]
        }
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }

    // 🌟 10-2. 버그 제보 / 피드백 상태 변경 버튼 클릭 (fb_st:<status>:<ticket_id>)
    if (customId.startsWith("fb_st:")) {
      const parts = customId.split(":");
      const targetStatus = parts[1];
      const ticketId = parts[2];

      const STATUS_LABEL_MAP = {
        "confirmed": "확인됨",
        "resolved": "해결됨",
        "implemented": "반영됨",
        "rejected": "반려됨"
      };
      const label = STATUS_LABEL_MAP[targetStatus] || targetStatus;
      const adminName = "개발자";
      const now = new Date().toISOString();

      let threadId = null;
      let channelId = null;
      let messageId = null;

      if (env.R2_BUCKET) {
        let queueData = { ticket_id: ticketId, status: targetStatus, status_label: label, pending_replies: [], status_history: [] };
        const qFile = await env.R2_BUCKET.get(`feedback/queue/${ticketId}.json`);
        if (qFile) {
          try { queueData = await qFile.json(); } catch (_) {}
        }

        queueData.status = targetStatus;
        queueData.status_label = label;
        queueData.status_history = queueData.status_history || [];
        queueData.status_history.push({
          status: targetStatus,
          label: label,
          timestamp: now,
          by: adminName
        });

        threadId = queueData.thread_id;
        channelId = queueData.channel_id;
        messageId = queueData.message_id;

        await env.R2_BUCKET.put(`feedback/queue/${ticketId}.json`, JSON.stringify(queueData), {
          httpMetadata: { contentType: "application/json" }
        });
      }

      // 디스코드 스레드에 상태 변경 알림 로그 자동 전송
      const botToken = env.DISCORD_BOT_TOKEN || DEFAULT_BOT_TOKEN;
      if (botToken && threadId) {
        try {
          await fetch(`https://discord.com/api/v10/channels/${threadId}/messages`, {
            method: "POST",
            headers: { "Authorization": `Bot ${botToken}`, "Content-Type": "application/json" },
            body: JSON.stringify({
              content: `📌 **개발자(@${adminName})에 의해 상태가 [${label}]으로 변경되었습니다.** (유저 앱 동기화 대기)`
            })
          });
        } catch (_) {}
      }

      // 원본 메시지 임베드 갱신
      const origEmbed = (interaction.message?.embeds && interaction.message.embeds[0]) || {};
      const statusColorMap = {
        "resolved": 0x10B981,
        "implemented": 0x10B981,
        "confirmed": 0x3B82F6,
        "rejected": 0x6B7280
      };
      const newColor = statusColorMap[targetStatus] || origEmbed.color || 0x3B82F6;

      const updatedEmbed = {
        ...origEmbed,
        color: newColor,
        description: (origEmbed.description || "") + `\n\n📌 **[상태 변경]** 개발자(@${adminName})에 의해 **[${label}]** 상태로 변경되었습니다.`
      };

      return new Response(JSON.stringify({
        type: 7,
        data: {
          embeds: [updatedEmbed],
          components: interaction.message?.components || []
        }
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }
  }

  // 🌟 10-3. 슬래시 명령어 (/답변, /reply) 처리
  if (interaction.type === 2) {
    const cmdName = interaction.data?.name;
    if (cmdName === "답변" || cmdName === "reply") {
      const channelId = interaction.channel_id;
      const options = interaction.data?.options || [];
      const replyContent = (options[0]?.value || "").trim();
      const adminName = "개발자";

      if (!replyContent) {
        return new Response(JSON.stringify({
          type: 4,
          data: { content: "⚠️ 답변 내용을 입력해 주세요.", flags: 64 }
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }

      let ticketId = null;
      if (env.R2_BUCKET) {
        const mapFile = await env.R2_BUCKET.get(`feedback/thread_map/${channelId}.json`);
        if (mapFile) {
          try {
            const mapData = await mapFile.json();
            ticketId = mapData.ticket_id;
          } catch (_) {}
        }
      }

      if (!ticketId) {
        return new Response(JSON.stringify({
          type: 4,
          data: { content: "⚠️ 해당 스레드와 연결된 제보 티켓을 찾을 수 없습니다. (제보 스레드 내부에서 실행하세요)", flags: 64 }
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }

      const now = new Date().toISOString();
      const newMsgId = `reply_${Date.now()}_${crypto.randomUUID().slice(0, 6)}`;

      if (env.R2_BUCKET) {
        let queueData = { ticket_id: ticketId, status: "reviewing", status_label: "확인중", pending_replies: [] };
        const qFile = await env.R2_BUCKET.get(`feedback/queue/${ticketId}.json`);
        if (qFile) {
          try { queueData = await qFile.json(); } catch (_) {}
        }

        queueData.pending_replies = queueData.pending_replies || [];
        queueData.pending_replies.push({
          msg_id: newMsgId,
          author: "개발자",
          content: replyContent,
          timestamp: now,
          delivered: false
        });

        await env.R2_BUCKET.put(`feedback/queue/${ticketId}.json`, JSON.stringify(queueData), {
          httpMetadata: { contentType: "application/json" }
        });
      }

      // 스레드에 답변 공개 메시지 등록
      const botToken = env.DISCORD_BOT_TOKEN || DEFAULT_BOT_TOKEN;
      if (botToken) {
        try {
          await fetch(`https://discord.com/api/v10/channels/${channelId}/messages`, {
            method: "POST",
            headers: { "Authorization": `Bot ${botToken}`, "Content-Type": "application/json" },
            body: JSON.stringify({
              content: `💬 **[개발자 답변]**\n>>> ${replyContent}`
            })
          });
        } catch (_) {}
      }

      return new Response(JSON.stringify({
        type: 4,
        data: {
          content: `📨 **[답변 대기열 등록 완료]** 유저의 번역기 앱으로 전달 대기 중입니다. 유저가 앱을 켜거나 확인하면 실시간 수신 영수증 로그가 여기에 출력됩니다.`
        }
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    }
  }

  return new Response(JSON.stringify({ type: 4, data: { content: "알 수 없는 요청입니다.", flags: 64 } }), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
}

// ------------------------------------------------------------------------------
// 11. POST /feedback/reply -> 유저 앱에서 디스코드 스레드로 답장 전송
// ------------------------------------------------------------------------------
async function handleUserReply(request, env, corsHeaders) {
  try {
    const data = await request.json();
    const threadId = data.thread_id;
    const replyText = (data.reply_text || "").trim();
    const botToken = env.DISCORD_BOT_TOKEN || DEFAULT_BOT_TOKEN;

    if (!threadId || !replyText || !botToken) {
      return new Response(JSON.stringify({ ok: false, error: "필수 파라미터가 누락되었습니다." }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" }
      });
    }

    const headers = {
      "Authorization": `Bot ${botToken}`,
      "Content-Type": "application/json"
    };

    // 스레드가 아카이브(닫힘) 상태라면 자동 잠금 해제
    try {
      await fetch(`https://discord.com/api/v10/channels/${threadId}`, {
        method: "PATCH",
        headers,
        body: JSON.stringify({ archived: false })
      });
    } catch (_) {}

    // 디스코드 스레드로 사용자 답장 게시
    const postResp = await fetch(`https://discord.com/api/v10/channels/${threadId}/messages`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        content: `💬 **[사용자 답장]**\n>>> ${replyText}`
      })
    });

    if (!postResp.ok) {
      const errText = await postResp.text();
      return new Response(JSON.stringify({ ok: false, error: errText }), {
        status: postResp.status,
        headers: { ...corsHeaders, "Content-Type": "application/json" }
      });
    }

    const msgData = await postResp.json();
    return new Response(JSON.stringify({
      ok: true,
      msg_id: msgData.id,
      timestamp: msgData.timestamp || new Date().toISOString()
    }), {
      status: 200,
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  } catch (err) {
    return new Response(JSON.stringify({ ok: false, error: String(err) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  }
}

// ------------------------------------------------------------------------------
// 11-2. POST /feedback/retract -> 유저의 제보 철회 (디스코드 메시지/스레드 및 큐 동시 삭제)
// ------------------------------------------------------------------------------
async function handleFeedbackRetract(request, env, corsHeaders) {
  try {
    const data = await request.json();
    const ticketId = data.ticket_id;
    let channelId = data.channel_id;
    let messageId = data.message_id;
    let threadId = data.thread_id;

    if (!ticketId) {
      return new Response(JSON.stringify({ ok: false, error: "ticket_id required" }), {
        status: 400,
        headers: { ...corsHeaders, "Content-Type": "application/json" }
      });
    }

    const botToken = env.DISCORD_BOT_TOKEN || DEFAULT_BOT_TOKEN;

    // 1. R2 큐에서 정보 보충 및 큐 파일 삭제
    if (env.R2_BUCKET) {
      const qKey = `feedback/queue/${ticketId}.json`;
      const qFile = await env.R2_BUCKET.get(qKey);
      if (qFile) {
        try {
          const qData = await qFile.json();
          channelId = channelId || qData.channel_id;
          messageId = messageId || qData.message_id;
          threadId = threadId || qData.thread_id;
        } catch (_) {}
      }

      await env.R2_BUCKET.delete(qKey);
      if (threadId) {
        await env.R2_BUCKET.delete(`feedback/thread_map/${threadId}.json`);
      }
    }

    // 2. 디스코드 스레드 및 원본 메시지 삭제
    if (botToken) {
      const headers = { "Authorization": `Bot ${botToken}` };

      // (A) 스레드 삭제
      if (threadId) {
        try {
          await fetch(`https://discord.com/api/v10/channels/${threadId}`, {
            method: "DELETE",
            headers
          });
        } catch (_) {}
      }

      // (B) 원본 메시지 삭제
      if (channelId && messageId) {
        try {
          await fetch(`https://discord.com/api/v10/channels/${channelId}/messages/${messageId}`, {
            method: "DELETE",
            headers
          });
        } catch (_) {}
      }
    }

    return new Response(JSON.stringify({ ok: true, retracted: true, ticket_id: ticketId }), {
      status: 200,
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  } catch (err) {
    return new Response(JSON.stringify({ ok: false, error: String(err) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  }
}

async function verifyDiscordSignature(request, env) {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");
  const body = await request.text();

  if (!signature || !timestamp) {
    return { isValid: false, body: "" };
  }

  const rawKey = (env.DISCORD_PUBLIC_KEY || "").trim();
  if (!rawKey) {
    return { isValid: true, body };
  }

  try {
    const publicKey = await crypto.subtle.importKey(
      "raw",
      hexToUint8Array(rawKey),
      { name: "Ed25519", namedCurve: "Ed25519" },
      false,
      ["verify"]
    );

    const encoder = new TextEncoder();
    const message = encoder.encode(timestamp + body);

    const isValid = await crypto.subtle.verify(
      { name: "Ed25519" },
      publicKey,
      hexToUint8Array(signature.trim()),
      message
    );

    return { isValid, body };
  } catch (err) {
    console.error("Signature verification error:", err);
    return { isValid: false, body };
  }
}

function hexToUint8Array(hex) {
  if (!hex) return new Uint8Array(0);
  const clean = hex.trim().replace(/^0x/i, "");
  const matches = clean.match(/.{1,2}/g);
  if (!matches) return new Uint8Array(0);
  return new Uint8Array(matches.map((b) => parseInt(b, 16)));
}

// ------------------------------------------------------------------------------
// 12. GET /discord/setup-commands -> 디스코드 슬래시 명령어(/답변, /reply) 일괄 자동 등록
// ------------------------------------------------------------------------------
async function handleSetupDiscordCommands(request, env, corsHeaders) {
  try {
    const botToken = env.DISCORD_BOT_TOKEN || DEFAULT_BOT_TOKEN;
    const appId = "1563511542568452157";

    const commands = [
      {
        name: "답변",
        description: "제보자(유저)에게 답변을 전송합니다.",
        options: [
          {
            name: "내용",
            description: "답변할 내용",
            type: 3,
            required: true
          }
        ]
      },
      {
        name: "reply",
        description: "Send a reply to the feedback ticket user.",
        options: [
          {
            name: "content",
            description: "Reply content",
            type: 3,
            required: true
          }
        ]
      }
    ];

    const resp = await fetch(`https://discord.com/api/v10/applications/${appId}/commands`, {
      method: "PUT",
      headers: {
        "Authorization": `Bot ${botToken}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(commands)
    });

    const resJson = await resp.json();
    return new Response(JSON.stringify({
      ok: resp.ok,
      status: resp.status,
      result: resJson
    }, null, 2), {
      status: resp.status,
      headers: { ...corsHeaders, "Content-Type": "application/json; charset=utf-8" }
    });
  } catch (err) {
    return new Response(JSON.stringify({ ok: false, error: String(err) }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" }
    });
  }
}
