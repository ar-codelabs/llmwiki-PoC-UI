(function () {
  "use strict";

  function apiFailure(status) {
    const suffix = Number.isInteger(status)
      ? ` (HTTP ${status})`
      : "";
    const error = new Error(
      `API 요청에 실패했습니다.${suffix}`,
    );
    if (Number.isInteger(status)) {
      error.status = status;
    }
    return error;
  }

  function isRecord(value) {
    return (
      value !== null &&
      typeof value === "object" &&
      !Array.isArray(value)
    );
  }

  function safePath(path) {
    if (
      typeof path !== "string" ||
      !path.startsWith("/api/") ||
      path.startsWith("//")
    ) {
      throw apiFailure();
    }
    let parsed;
    try {
      parsed = new URL(path, window.location.origin);
    } catch (error) {
      throw apiFailure();
    }
    if (
      parsed.origin !== window.location.origin ||
      !parsed.pathname.startsWith("/api/") ||
      parsed.hash ||
      parsed.username ||
      parsed.password
    ) {
      throw apiFailure();
    }
    return `${parsed.pathname}${parsed.search}`;
  }

  function validateResponseOrigin(response, path) {
    if (
      !response ||
      typeof response.ok !== "boolean" ||
      typeof response.status !== "number" ||
      typeof response.url !== "string" ||
      !response.url
    ) {
      throw apiFailure();
    }
    const expected = new URL(path, window.location.origin);
    let actual;
    try {
      actual = new URL(response.url);
    } catch (error) {
      throw apiFailure(response.status);
    }
    if (
      actual.origin !== expected.origin ||
      actual.pathname !== expected.pathname ||
      actual.search !== expected.search ||
      actual.hash
    ) {
      throw apiFailure(response.status);
    }
  }

  async function request(path, method, body) {
    const target = safePath(path);
    const token =
      window.DemoAuth &&
      typeof window.DemoAuth.accessToken === "function"
        ? window.DemoAuth.accessToken()
        : null;
    if (typeof token !== "string" || !token) {
      throw apiFailure(401);
    }

    const headers = new Headers();
    headers.set("Accept", "application/json");
    headers.set("Authorization", `Bearer ${token}`);
    headers.set("Content-Type", "application/json");
    headers.set("X-Demo-Request", "1");
    const options = {
      method,
      headers,
      cache: "no-store",
      credentials: "same-origin",
      redirect: "error",
    };
    if (method === "POST") {
      if (!isRecord(body)) {
        throw apiFailure();
      }
      options.body = JSON.stringify(body);
    }

    let response;
    try {
      response = await fetch(target, options);
    } catch (error) {
      throw apiFailure();
    }
    validateResponseOrigin(response, target);

    const contentType =
      response.headers &&
      typeof response.headers.get === "function"
        ? response.headers.get("content-type")
        : null;
    if (
      typeof contentType !== "string" ||
      !/^application\/json(?:\s*;|$)/i.test(contentType)
    ) {
      throw apiFailure(response.status);
    }

    let responseBody;
    try {
      responseBody = await response.json();
    } catch (error) {
      throw apiFailure(response.status);
    }
    if (!response.ok || !isRecord(responseBody)) {
      throw apiFailure(response.status);
    }
    return responseBody;
  }

  function get(path) {
    return request(path, "GET");
  }

  function post(path, body) {
    return request(path, "POST", body);
  }

  window.DemoApi = Object.freeze({
    get,
    post,
  });
})();
