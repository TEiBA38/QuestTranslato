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
};
