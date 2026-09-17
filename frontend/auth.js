(function () {
  "use strict";

  const STORAGE = Object.freeze({
    accessToken: "demo.auth.access_token",
    expiresAt: "demo.auth.expires_at",
    subject: "demo.auth.subject",
    verifier: "demo.auth.pkce_verifier",
    state: "demo.auth.oauth_state",
    nonce: "demo.auth.nonce",
    returnTo: "demo.auth.return_to",
  });
  const CONFIG_FIELDS = Object.freeze([
    "app_client_id",
    "callback_path",
    "cognito_domain",
    "region",
    "user_pool_id",
  ]);
  const BASE64URL = /^[A-Za-z0-9_-]+$/;
  let configPromise = null;

  function authFailure() {
    return new Error("인증을 완료하지 못했습니다.");
  }

  function isRecord(value) {
    return (
      value !== null &&
      typeof value === "object" &&
      !Array.isArray(value)
    );
  }

  function readStorage(key) {
    try {
      return sessionStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  function writeStorage(key, value) {
    try {
      sessionStorage.setItem(key, value);
    } catch (error) {
      throw authFailure();
    }
  }

  function removeStorage(key) {
    try {
      sessionStorage.removeItem(key);
    } catch (error) {
      // Storage cleanup is best effort after a fail-closed decision.
    }
  }

  function clearFlow() {
    removeStorage(STORAGE.verifier);
    removeStorage(STORAGE.state);
    removeStorage(STORAGE.nonce);
    removeStorage(STORAGE.returnTo);
  }

  function clearAuth() {
    removeStorage(STORAGE.accessToken);
    removeStorage(STORAGE.expiresAt);
    removeStorage(STORAGE.subject);
  }

  function clearSession() {
    clearFlow();
    clearAuth();
  }

  function testAuth() {
    const injected = window.__DEMO_TEST_AUTH__;
    if (
      !isRecord(injected) ||
      typeof injected.accessToken !== "string" ||
      !injected.accessToken
    ) {
      return null;
    }
    return {
      accessToken: injected.accessToken,
      subject:
        typeof injected.subject === "string" &&
        injected.subject
          ? injected.subject
          : "local-test-user",
    };
  }

  function base64url(bytes) {
    let binary = "";
    for (const byte of bytes) {
      binary += String.fromCharCode(byte);
    }
    return btoa(binary)
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/g, "");
  }

  function randomValue() {
    const bytes = new Uint8Array(32);
    window.crypto.getRandomValues(bytes);
    return base64url(bytes);
  }

  async function codeChallenge(verifier) {
    const digest = await window.crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(verifier),
    );
    return base64url(new Uint8Array(digest));
  }

  function expectedResponseUrl(expectedUrl) {
    const expected = new URL(
      expectedUrl,
      window.location.origin,
    );
    if (
      expected.username ||
      expected.password ||
      expected.hash
    ) {
      throw authFailure();
    }
    return expected;
  }

  function validateResponseOrigin(response, expectedUrl) {
    if (
      !response ||
      typeof response.ok !== "boolean" ||
      typeof response.status !== "number" ||
      typeof response.url !== "string" ||
      !response.url
    ) {
      throw authFailure();
    }
    const expected = expectedResponseUrl(expectedUrl);
    let actual;
    try {
      actual = new URL(response.url);
    } catch (error) {
      throw authFailure();
    }
    if (
      actual.origin !== expected.origin ||
      actual.pathname !== expected.pathname ||
      actual.search !== expected.search ||
      actual.hash
    ) {
      throw authFailure();
    }
  }

  async function jsonResponse(response, expectedUrl) {
    validateResponseOrigin(response, expectedUrl);
    const contentType =
      response.headers &&
      typeof response.headers.get === "function"
        ? response.headers.get("content-type")
        : null;
    if (
      typeof contentType !== "string" ||
      !/^application\/json(?:\s*;|$)/i.test(contentType)
    ) {
      throw authFailure();
    }
    let body;
    try {
      body = await response.json();
    } catch (error) {
      throw authFailure();
    }
    if (!response.ok || !isRecord(body)) {
      throw authFailure();
    }
    return body;
  }

  function nonEmptyString(value, maximum = 512) {
    return (
      typeof value === "string" &&
      value.length > 0 &&
      value.length <= maximum &&
      value.trim() === value
    );
  }

  function validateConfig(body) {
    if (!isRecord(body)) {
      throw authFailure();
    }
    const keys = Object.keys(body).sort();
    if (
      keys.length !== CONFIG_FIELDS.length ||
      keys.some((key, index) => key !== CONFIG_FIELDS[index])
    ) {
      throw authFailure();
    }
    for (const field of CONFIG_FIELDS) {
      if (!nonEmptyString(body[field])) {
        throw authFailure();
      }
    }
    if (
      !/^[a-z]{2}(?:-gov)?-[a-z0-9-]+-\d+$/.test(
        body.region,
      ) ||
      !/^[A-Za-z0-9_-]+$/.test(body.user_pool_id) ||
      !/^[A-Za-z0-9_-]+$/.test(body.app_client_id)
    ) {
      throw authFailure();
    }

    let domain;
    try {
      domain = new URL(body.cognito_domain);
    } catch (error) {
      throw authFailure();
    }
    if (
      domain.protocol !== "https:" ||
      !domain.hostname.endsWith(".amazoncognito.com") ||
      domain.pathname !== "/" ||
      domain.search ||
      domain.hash ||
      domain.username ||
      domain.password
    ) {
      throw authFailure();
    }

    let callback;
    try {
      callback = new URL(
        body.callback_path,
        window.location.origin,
      );
    } catch (error) {
      throw authFailure();
    }
    if (
      !body.callback_path.startsWith("/") ||
      body.callback_path.startsWith("//") ||
      callback.origin !== window.location.origin ||
      callback.pathname !== body.callback_path ||
      callback.search ||
      callback.hash
    ) {
      throw authFailure();
    }

    return Object.freeze({
      region: body.region,
      user_pool_id: body.user_pool_id,
      app_client_id: body.app_client_id,
      cognito_domain: domain.origin,
      callback_path: callback.pathname,
    });
  }

  async function loadConfig() {
    if (!configPromise) {
      configPromise = (async function () {
        const response = await fetch("/api/config", {
          method: "GET",
          headers: {
            Accept: "application/json",
          },
          cache: "no-store",
          credentials: "same-origin",
          redirect: "error",
        });
        const body = await jsonResponse(
          response,
          new URL("/api/config", window.location.origin),
        );
        return validateConfig(body);
      })().catch(function () {
        configPromise = null;
        throw authFailure();
      });
    }
    return configPromise;
  }

  function safeReturnPath(value) {
    if (
      typeof value !== "string" ||
      !value.startsWith("/") ||
      value.startsWith("//")
    ) {
      return "/";
    }
    let target;
    try {
      target = new URL(value, window.location.origin);
    } catch (error) {
      return "/";
    }
    if (target.origin !== window.location.origin) {
      return "/";
    }
    return `${target.pathname}${target.search}${target.hash}`;
  }

  function currentReturnPath(callbackPath) {
    if (window.location.pathname === callbackPath) {
      return "/";
    }
    return safeReturnPath(
      `${window.location.pathname}${window.location.search}` +
        `${window.location.hash}`,
    );
  }

  function decodeJwt(token) {
    if (
      typeof token !== "string" ||
      token.length > 16384
    ) {
      throw authFailure();
    }
    const parts = token.split(".");
    if (
      parts.length !== 3 ||
      !parts.every(
        (part) => part && BASE64URL.test(part),
      )
    ) {
      throw authFailure();
    }
    const payload = parts[1]
      .replace(/-/g, "+")
      .replace(/_/g, "/");
    const padding = "=".repeat((4 - (payload.length % 4)) % 4);
    let claims;
    try {
      claims = JSON.parse(atob(payload + padding));
    } catch (error) {
      throw authFailure();
    }
    if (!isRecord(claims)) {
      throw authFailure();
    }
    return claims;
  }

  function validSubject(value) {
    return (
      typeof value === "string" &&
      value.length > 0 &&
      value.length <= 128 &&
      /^[A-Za-z0-9._:@-]+$/.test(value)
    );
  }

  function validateTokenResponse(
    body,
    config,
    expectedNonce,
  ) {
    if (
      typeof body.access_token !== "string" ||
      typeof body.id_token !== "string" ||
      body.token_type !== "Bearer" ||
      !Number.isInteger(body.expires_in) ||
      body.expires_in <= 0 ||
      body.expires_in > 86400
    ) {
      throw authFailure();
    }
    const accessClaims = decodeJwt(body.access_token);
    const idClaims = decodeJwt(body.id_token);
    const now = Math.floor(Date.now() / 1000);
    if (
      accessClaims.token_use !== "access" ||
      accessClaims.client_id !== config.app_client_id ||
      !validSubject(accessClaims.sub) ||
      !Number.isInteger(accessClaims.exp) ||
      accessClaims.exp <= now ||
      idClaims.token_use !== "id" ||
      idClaims.aud !== config.app_client_id ||
      idClaims.nonce !== expectedNonce ||
      idClaims.sub !== accessClaims.sub ||
      !Number.isInteger(idClaims.exp) ||
      idClaims.exp <= now
    ) {
      throw authFailure();
    }
    return {
      accessToken: body.access_token,
      subject: accessClaims.sub,
      expiresAt: Math.min(
        accessClaims.exp * 1000,
        Date.now() + body.expires_in * 1000,
      ),
    };
  }

  function accessToken() {
    const injected = testAuth();
    if (injected) {
      return injected.accessToken;
    }
    const token = readStorage(STORAGE.accessToken);
    const rawExpiresAt = readStorage(STORAGE.expiresAt);
    const expiresAt = Number(rawExpiresAt);
    if (
      typeof token !== "string" ||
      !token ||
      !Number.isFinite(expiresAt) ||
      expiresAt <= Date.now() + 5000
    ) {
      clearAuth();
      return null;
    }
    return token;
  }

  function subject() {
    const injected = testAuth();
    if (injected) {
      return injected.subject;
    }
    const value = readStorage(STORAGE.subject);
    return validSubject(value) ? value : null;
  }

  async function login() {
    const config = await loadConfig();
    clearSession();
    const verifier = randomValue();
    const state = randomValue();
    const nonce = randomValue();
    const challenge = await codeChallenge(verifier);
    const redirectUri =
      window.location.origin + config.callback_path;

    writeStorage(STORAGE.verifier, verifier);
    writeStorage(STORAGE.state, state);
    writeStorage(STORAGE.nonce, nonce);
    writeStorage(
      STORAGE.returnTo,
      currentReturnPath(config.callback_path),
    );

    const authorize = new URL(
      "/oauth2/authorize",
      config.cognito_domain,
    );
    authorize.searchParams.set(
      "client_id",
      config.app_client_id,
    );
    authorize.searchParams.set("response_type", "code");
    authorize.searchParams.set("scope", "openid");
    authorize.searchParams.set("redirect_uri", redirectUri);
    authorize.searchParams.set("state", state);
    authorize.searchParams.set("nonce", nonce);
    authorize.searchParams.set(
      "code_challenge_method",
      "S256",
    );
    authorize.searchParams.set("code_challenge", challenge);
    window.location.assign(authorize.toString());
    return null;
  }

  async function bootstrap() {
    const injected = testAuth();
    if (injected) {
      return injected;
    }
    const token = accessToken();
    const currentSubject = subject();
    if (token && currentSubject) {
      return {
        accessToken: token,
        subject: currentSubject,
      };
    }
    await login();
    return null;
  }

  async function callback() {
    try {
      const config = await loadConfig();
      const current = new URL(window.location.href);
      if (
        current.origin !== window.location.origin ||
        current.pathname !== config.callback_path ||
        current.hash
      ) {
        throw authFailure();
      }

      const parameters = current.searchParams;
      const codes = parameters.getAll("code");
      const states = parameters.getAll("state");
      const storedState = readStorage(STORAGE.state);
      const verifier = readStorage(STORAGE.verifier);
      const expectedNonce = readStorage(STORAGE.nonce);
      const returnTo = safeReturnPath(
        readStorage(STORAGE.returnTo),
      );
      if (
        parameters.has("error") ||
        codes.length !== 1 ||
        states.length !== 1 ||
        !codes[0] ||
        !states[0] ||
        !storedState ||
        states[0] !== storedState ||
        typeof verifier !== "string" ||
        verifier.length < 43 ||
        verifier.length > 128 ||
        !BASE64URL.test(verifier) ||
        typeof expectedNonce !== "string" ||
        !expectedNonce ||
        !BASE64URL.test(expectedNonce)
      ) {
        throw authFailure();
      }

      const tokenUrl = new URL(
        "/oauth2/token",
        config.cognito_domain,
      );
      const form = new URLSearchParams({
        grant_type: "authorization_code",
        client_id: config.app_client_id,
        code: codes[0],
        redirect_uri:
          window.location.origin + config.callback_path,
        code_verifier: verifier,
      });
      const response = await fetch(tokenUrl.toString(), {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type":
            "application/x-www-form-urlencoded;charset=UTF-8",
        },
        body: form.toString(),
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
      });
      const body = await jsonResponse(response, tokenUrl);
      const token = validateTokenResponse(
        body,
        config,
        expectedNonce,
      );

      clearSession();
      writeStorage(STORAGE.accessToken, token.accessToken);
      writeStorage(STORAGE.subject, token.subject);
      writeStorage(
        STORAGE.expiresAt,
        String(token.expiresAt),
      );
      if (
        window.history &&
        typeof window.history.replaceState === "function"
      ) {
        window.history.replaceState(
          null,
          "",
          config.callback_path,
        );
      }
      window.location.replace(returnTo);
      return {
        accessToken: token.accessToken,
        subject: token.subject,
      };
    } catch (error) {
      clearSession();
      throw authFailure();
    }
  }

  async function logout() {
    const injected = testAuth();
    if (injected) {
      window.__DEMO_TEST_AUTH__ = null;
    }
    clearSession();
    if (injected) {
      window.location.assign("/");
      return;
    }
    let config;
    try {
      config = await loadConfig();
    } catch (error) {
      window.location.replace("/");
      return;
    }
    const logoutUrl = new URL(
      "/logout",
      config.cognito_domain,
    );
    logoutUrl.searchParams.set(
      "client_id",
      config.app_client_id,
    );
    logoutUrl.searchParams.set(
      "logout_uri",
      window.location.origin + "/",
    );
    window.location.assign(logoutUrl.toString());
  }

  window.DemoAuth = Object.freeze({
    bootstrap,
    login,
    callback,
    logout,
    accessToken,
    subject,
    validSubject,
  });
})();
