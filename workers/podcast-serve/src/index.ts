interface Env {
  BUCKET: R2Bucket;
}

interface ByteRange {
  offset: number;
  length: number;
  contentRange: string;
}

function contentTypeForKey(key: string): string {
  if (key.endsWith(".xml")) {
    return "application/rss+xml; charset=utf-8";
  }
  if (key.endsWith(".mp3")) {
    return "audio/mpeg";
  }
  if (key.endsWith(".jpg") || key.endsWith(".jpeg")) {
    return "image/jpeg";
  }
  if (key.endsWith(".png")) {
    return "image/png";
  }
  return "application/octet-stream";
}

function cacheControlForKey(key: string): string {
  if (key.endsWith(".xml")) {
    return "public, max-age=300";
  }
  if (key.endsWith(".mp3")) {
    return "public, max-age=3600";
  }
  return "public, max-age=86400";
}

function parseRangeHeader(rangeHeader: string, size: number): ByteRange | null {
  const match = /^bytes=(\d*)-(\d*)$/.exec(rangeHeader.trim());
  if (!match) {
    return null;
  }

  const startRaw = match[1];
  const endRaw = match[2];
  let start: number;
  let end: number;

  if (startRaw === "" && endRaw === "") {
    return null;
  }

  if (startRaw === "") {
    const suffixLength = Number.parseInt(endRaw, 10);
    if (!Number.isFinite(suffixLength) || suffixLength <= 0) {
      return null;
    }
    start = Math.max(size - suffixLength, 0);
    end = size - 1;
  } else {
    start = Number.parseInt(startRaw, 10);
    if (!Number.isFinite(start) || start < 0 || start >= size) {
      return null;
    }
    if (endRaw === "") {
      end = size - 1;
    } else {
      end = Number.parseInt(endRaw, 10);
      if (!Number.isFinite(end) || end < start) {
        return null;
      }
      end = Math.min(end, size - 1);
    }
  }

  const length = end - start + 1;
  return {
    offset: start,
    length,
    contentRange: `bytes ${start}-${end}/${size}`,
  };
}

function normalizePathToKey(pathname: string): string | null {
  if (pathname === "/" || pathname === "") {
    return "feed.xml";
  }
  if (pathname === "/feed.xml") {
    return "feed.xml";
  }
  if (pathname.startsWith("/feeds/") && pathname.endsWith(".xml")) {
    return pathname.slice(1);
  }
  if (pathname.startsWith("/episodes/") && pathname.endsWith(".mp3")) {
    return pathname.slice(1);
  }
  if (pathname.startsWith("/cover") && (pathname.endsWith(".jpg") || pathname.endsWith(".jpeg") || pathname.endsWith(".png"))) {
    return pathname.slice(1);
  }
  return null;
}

function methodNotAllowed(): Response {
  return new Response("Method Not Allowed", {
    status: 405,
    headers: {
      Allow: "GET, HEAD",
    },
  });
}

function notFound(): Response {
  return new Response("Not Found", { status: 404 });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return methodNotAllowed();
    }

    const url = new URL(request.url);
    const key = normalizePathToKey(url.pathname);
    if (!key) {
      return notFound();
    }

    const isAudio = key.endsWith(".mp3");
    const rangeHeader = request.headers.get("Range");
    const objectHead = await env.BUCKET.head(key);
    if (!objectHead) {
      return notFound();
    }

    let status = 200;
    let body: ReadableStream | null = null;
    const headers = new Headers();
    headers.set("Content-Type", contentTypeForKey(key));
    headers.set("Cache-Control", cacheControlForKey(key));
    if (isAudio) {
      headers.set("Accept-Ranges", "bytes");
    }
    if (objectHead.httpEtag) {
      headers.set("ETag", objectHead.httpEtag);
    }

    if (request.method === "HEAD") {
      headers.set("Content-Length", String(objectHead.size));
      return new Response(null, { status, headers });
    }

    if (isAudio && rangeHeader) {
      const parsedRange = parseRangeHeader(rangeHeader, objectHead.size);
      if (!parsedRange) {
        headers.set("Content-Range", `bytes */${objectHead.size}`);
        return new Response("Range Not Satisfiable", { status: 416, headers });
      }

      const rangedObject = await env.BUCKET.get(key, {
        range: {
          offset: parsedRange.offset,
          length: parsedRange.length,
        },
      });
      if (!rangedObject) {
        return notFound();
      }

      status = 206;
      body = rangedObject.body;
      headers.set("Content-Length", String(parsedRange.length));
      headers.set("Content-Range", parsedRange.contentRange);
      return new Response(body, { status, headers });
    }

    const objectBody = await env.BUCKET.get(key);
    if (!objectBody) {
      return notFound();
    }
    body = objectBody.body;
    headers.set("Content-Length", String(objectBody.size));

    return new Response(body, { status, headers });
  },
};
