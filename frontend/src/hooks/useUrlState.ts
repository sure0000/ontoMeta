import { useCallback, useEffect, useMemo, useRef } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * 把一段界面状态放进 URL 查询串。
 *
 * 为什么不是 useState：工作区/审核里的「看哪个板块、翻到第几页、开着哪个 Tab」
 * 一旦只活在组件里，点进详情页再返回就全丢——审核尤其致命，因为每判一个对象都要
 * 返回一次。放进 URL 后返回、刷新、分享链接都能回到原位。
 *
 * 写入一律 `replace`：这些是同一屏内的视图状态，不该在浏览器历史里堆出几十条记录，
 * 否则「后退」变成逐格回放筛选器。多个键在同一 tick 内写入时用函数式更新合并，
 * 避免各自基于旧快照互相覆盖。
 */
/**
 * 同一 tick 内的写入缓冲。
 *
 * 一次交互里连着改两个参数是常事——换板块要顺带清游标、翻页要顺带改页长。而
 * react-router 的 `setSearchParams` 每次都从**它自己记住的那份快照**算起，快照要等
 * 导航生效后才更新：于是同一 tick 的第二次写是基于旧值算的，把第一次写的键抹掉。
 *
 * 症状很隐蔽——点板块「没有反应」：`segment` 刚写进 URL，紧随其后的 `cursor` 清除
 * 就用不含它的旧快照覆盖了回去。
 *
 * 这里让本 tick 内的后续写入叠加在前一次的结果上，微任务结束即丢弃缓冲、下一 tick
 * 重新以路由为准。应用里只有一个 Router，故用模块级即可。
 */
let pendingParams: URLSearchParams | null = null;

function useSetParams() {
  const [searchParams, setSearchParams] = useSearchParams();
  return useCallback(
    (patch: Record<string, string | null>) => {
      const next = pendingParams ?? new URLSearchParams(searchParams);
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === "") next.delete(key);
        else next.set(key, value);
      }
      if (pendingParams === null) {
        pendingParams = next;
        queueMicrotask(() => {
          pendingParams = null;
        });
      }
      setSearchParams(new URLSearchParams(next), { replace: true });
    },
    [searchParams, setSearchParams],
  );
}

/** 一次原子地改多个查询参数（值传 null 表示删除）。 */
export function useUrlParams() {
  return useSetParams();
}

/** URL 中的字符串状态；值等于 fallback 时不写进 URL，保持链接干净。 */
export function useUrlState<T extends string>(
  key: string,
  fallback: T,
  allowed?: readonly T[],
): [T, (value: T) => void] {
  const [searchParams] = useSearchParams();
  const setParams = useSetParams();
  const raw = searchParams.get(key);
  const value = useMemo(() => {
    if (raw == null) return fallback;
    if (allowed && !allowed.includes(raw as T)) return fallback;
    return raw as T;
  }, [raw, fallback, allowed]);
  const set = useCallback(
    (next: T) => setParams({ [key]: next === fallback ? null : next }),
    [setParams, key, fallback],
  );
  return [value, set];
}

/** URL 中的数字状态（页码等）。非法值回落到 fallback，不让脏参数把页面打空。 */
export function useUrlNumber(
  key: string,
  fallback: number,
): [number, (value: number) => void] {
  const [searchParams] = useSearchParams();
  const setParams = useSetParams();
  const raw = searchParams.get(key);
  const parsed = raw == null ? NaN : Number(raw);
  const value = Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  const set = useCallback(
    (next: number) => setParams({ [key]: next === fallback ? null : String(next) }),
    [setParams, key, fallback],
  );
  return [value, set];
}

/**
 * 依赖变化时执行，但**跳过挂载那一次**。
 *
 * 「筛选变了就回到第 1 页」这类效应挂载时也会跑一遍，于是从 URL 恢复出来的页码
 * 会被立刻重置成 1——恢复等于没恢复。
 */
export function useEffectAfterMount(effect: () => void, deps: unknown[]) {
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    effect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
