// Cloudflare Workers - 디시인사이드 차단 우회용

export default {
  async fetch(request, env, ctx) {
    // CORS 헤더
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    // OPTIONS 요청 처리
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders });
    }

    try {
      // URL 파라미터에서 target URL 추출
      const url = new URL(request.url);
      const targetUrl = url.searchParams.get('url');
      
      if (!targetUrl) {
        return new Response('Missing url parameter', { status: 400 });
      }

      // DCInside만 허용 (보안)
      if (!targetUrl.includes('dcinside.com')) {
        return new Response('Only dcinside.com allowed', { status: 403 });
      }

      // 실제 브라우저 헤더로 DCInside 요청
      const dcHeaders = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Referer': 'https://www.dcinside.com/',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
      };

      // DCInside에 요청
      const response = await fetch(targetUrl, {
        headers: dcHeaders,
        cf: {
          // Cloudflare 최적화
          cacheEverything: false,
          cacheTtl: 0,
        },
      });

      // 응답 반환 (CORS 헤더 추가)
      const newHeaders = new Headers(response.headers);
      Object.keys(corsHeaders).forEach(key => {
        newHeaders.set(key, corsHeaders[key]);
      });

      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: newHeaders,
      });

    } catch (error) {
      return new Response(`Proxy error: ${error.message}`, { 
        status: 500,
        headers: corsHeaders,
      });
    }
  },
};
