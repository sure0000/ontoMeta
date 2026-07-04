import { useCallback, useEffect, useRef, useState } from "react";

export interface UseAsyncResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  /** 重新触发传入的 fetcher，返回最新结果。 */
  reload: () => Promise<T | null>;
  /** 直接覆盖本地 data，便于乐观更新。 */
  setData: (data: T | null) => void;
}

/**
 * 统一管理异步请求的 loading / error / data 状态。
 *
 * - 自动处理组件卸载：卸载后不会触发 setState，避免内存泄漏警告。
 * - 内部维护 mounted ref，通过 AbortController 取消 fetch（仅对支持 abort 的 fetcher 生效）。
 * - 支持依赖变化时自动重新请求。
 *
 * @param fetcher 接收一个 AbortSignal，返回 Promise<T>。如果不需要取消可忽略该参数。
 * @param deps 依赖数组，依赖变化时重新请求；为空数组则只在挂载时请求一次。
 */
export function useApi<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: unknown[] = [],
): UseAsyncResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  // 保存最新 fetcher 引用以便 reload 复用，避免依赖 fetcher 函数本身变化
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const run = useCallback(async (): Promise<T | null> => {
    const controller = new AbortController();
    if (!mountedRef.current) {
      controller.abort();
      return null;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await fetcherRef.current(controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return null;
      setData(result);
      return result;
    } catch (err) {
      if (!mountedRef.current || controller.signal.aborted) return null;
      // AbortError 视为正常取消
      if (err instanceof DOMException && err.name === "AbortError") return null;
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      return null;
    } finally {
      if (mountedRef.current && !controller.signal.aborted) {
        setLoading(false);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void run();
    return () => {
      mountedRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, loading, error, reload: run, setData };
}

/**
 * 返回一个带 debounce 的回调，常用于搜索输入触发远程查询。
 *
 * @param fn 目标回调
 * @param delay 延迟毫秒（默认 300ms）
 */
export function useDebouncedCallback<A extends unknown[]>(
  fn: (...args: A) => void,
  delay = 300,
): (...args: A) => void {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  return useCallback(
    (...args: A) => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => fnRef.current(...args), delay);
    },
    [delay],
  );
}
