/** A stand-in for the orchestrator's API: answers `fetch` from a table and records the calls. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { ToastProvider } from "../components/toast";

export interface Call {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
  init: RequestInit;
}

type Answer = { status?: number; body?: unknown } | ((call: Call) => { status?: number; body?: unknown });

export function mockApi(routes: Record<string, Answer>) {
  const calls: Call[] = [];
  const table = { ...routes };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://panel.test");
    const call: Call = {
      method: init.method ?? "GET",
      path: url.pathname,
      query: url.searchParams,
      body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
      init,
    };
    calls.push(call);
    const answer = table[`${call.method} ${call.path}`];
    if (!answer) return new Response(JSON.stringify({ detail: `no mock for ${call.method} ${call.path}` }), { status: 404 });
    const { status = 200, body } = typeof answer === "function" ? answer(call) : answer;
    return new Response(status === 204 ? null : JSON.stringify(body ?? null), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    /** Calls to one endpoint, e.g. "POST /api/config/reload". */
    to: (route: string) => calls.filter((call) => `${call.method} ${call.path}` === route),
    set: (route: string, answer: Answer) => {
      table[route] = answer;
    },
  };
}

export function renderApp(ui: ReactElement, { route = "/" }: { route?: string } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ToastProvider>{ui}</ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
