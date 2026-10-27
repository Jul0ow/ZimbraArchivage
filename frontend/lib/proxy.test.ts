import { afterEach, describe, expect, it, vi } from "vitest";

import { backend, backendUrl, filterHeaders, proxy } from "@/lib/proxy";

const context = (path: string[]) => ({ params: Promise.resolve({ path }) });

afterEach(() => {
  vi.restoreAllMocks();
});

describe("backendUrl", () => {
  it("préfixe le chemin par /api et encode les segments", () => {
    expect(backendUrl(["export", "a/b"], "?folder_id=1", "http://backend:8000")?.toString()).toBe(
      "http://backend:8000/api/export/a%2Fb?folder_id=1",
    );
  });

  it.each([[[".."]], [["..", "health"]], [["."]], [["auth", ""]]])("refuse %j", (path) => {
    expect(backendUrl(path, "", "http://backend:8000")).toBeNull();
  });
});

describe("filterHeaders", () => {
  it("retire les en-têtes propres à la connexion et garde les Set-Cookie multiples", () => {
    const source = new Headers({ connection: "keep-alive", host: "x", "content-length": "3" });
    source.append("set-cookie", "a=1");
    source.append("set-cookie", "b=2");

    const headers = filterHeaders(source);

    expect(headers.get("connection")).toBeNull();
    expect(headers.get("host")).toBeNull();
    expect(headers.get("content-length")).toBeNull();
    expect(headers.getSetCookie()).toEqual(["a=1", "b=2"]);
  });
});

describe("proxy", () => {
  it("relaie le corps en flux, sans le mettre en tampon", async () => {
    const body = new ReadableStream<Uint8Array>();
    const fetchMock = vi
      .spyOn(backend, "fetch")
      .mockResolvedValue(new Response(body, { status: 200 }) as never);

    const response = await proxy(new Request("http://app/api/export?folder_id=1"), context(["export"]));

    expect(response.status).toBe(200);
    expect(response.body).toBe(body);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url.pathname).toBe("/api/export");
    expect(url.search).toBe("?folder_id=1");
    expect(init?.signal).toBeDefined();
    expect(init?.redirect).toBe("manual");
  });

  it("répond 502 si le backend est injoignable", async () => {
    vi.spyOn(backend, "fetch").mockRejectedValue(new TypeError("fetch failed"));

    const response = await proxy(new Request("http://app/api/folders"), context(["folders"]));

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ detail: "Service d'archivage injoignable" });
  });

  it("refuse un chemin qui sort de /api", async () => {
    const fetchMock = vi.spyOn(backend, "fetch");

    const response = await proxy(new Request("http://app/api/x"), context(["..", "x"]));

    expect(response.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("transmet le corps d'un POST", async () => {
    const fetchMock = vi.spyOn(backend, "fetch").mockResolvedValue(new Response(null, { status: 204 }) as never);

    await proxy(
      new Request("http://app/api/auth/login", { method: "POST", body: '{"username":"a"}' }),
      context(["auth", "login"]),
    );

    const init = fetchMock.mock.calls[0][1];
    expect(init?.method).toBe("POST");
    expect(new TextDecoder().decode(init?.body as ArrayBuffer)).toBe('{"username":"a"}');
  });
});
