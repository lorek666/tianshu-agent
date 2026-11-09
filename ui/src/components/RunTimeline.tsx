import { useEffect, useRef } from "react";
import type { TimelineEntry } from "../lib/humanize";
import { actorLabel, actionLabel, eventDetail } from "../lib/humanize";
import { formatClock } from "../lib/format";
import { EmptyState } from "./StatusPill";

export interface RunTimelineProps {
  entries: TimelineEntry[];
  hasMore: boolean;
  live: boolean;
}

const PAYLOAD_LIMIT = 6_000;

function prettyPayload(payload: Record<string, unknown>): string {
  let text: string;
  try {
    text = JSON.stringify(payload, null, 2);
  } catch {
    text = "（payload 无法序列化）";
  }
  if (text.length <= PAYLOAD_LIMIT) return text;
  return `${text.slice(0, PAYLOAD_LIMIT)}\n…（payload 过长，已截断）`;
}

export function RunTimeline({ entries, hasMore, live }: RunTimelineProps) {
  const listRef = useRef<HTMLOListElement>(null);
  const stickToBottom = useRef(true);

  useEffect(() => {
    const list = listRef.current;
    if (list !== null && stickToBottom.current) {
      list.scrollTop = list.scrollHeight;
    }
  }, [entries.length]);

  const handleScroll = (): void => {
    const list = listRef.current;
    if (list === null) return;
    const distance = list.scrollHeight - list.scrollTop - list.clientHeight;
    stickToBottom.current = distance < 48;
  };

  return (
    <section className="timeline-section">
      <div className="section-title timeline-title">
        事件时间线
        <span className="section-hint">
          {live ? "实时接收事件（断线会自动重连）" : "每 3 秒自动刷新"}
        </span>
      </div>
      {hasMore ? (
        <div className="timeline-note">只保留了最近的事件；更早的事件未加载，不影响当前状态。</div>
      ) : null}
      {entries.length === 0 ? (
        <EmptyState title="还没有事件" hint="运行推进时，模型调用、文件操作和审批会逐条出现在这里。" />
      ) : (
        <ol className="timeline" ref={listRef} onScroll={handleScroll}>
          {entries.map((entry) => {
            const detail = eventDetail(entry.raw);
            return (
              <li key={entry.seq} className={`timeline-item tone-${entry.tone}`}>
                <span className="timeline-dot" aria-hidden="true" />
                <div className="timeline-content">
                  <div className="timeline-line">
                    <span className="timeline-time num">{formatClock(entry.ts)}</span>
                    <span className="timeline-text">{entry.sentence}</span>
                  </div>
                  <details className="timeline-details">
                    <summary>详情</summary>
                    <dl className="details-grid">
                      <dt>事件类型</dt>
                      <dd>
                        <code>{detail.type}</code>
                      </dd>
                      {detail.ruleId !== null ? (
                        <>
                          <dt>规则</dt>
                          <dd>
                            <code>{detail.ruleId}</code>（{actionLabel(detail.ruleId)}）
                          </dd>
                        </>
                      ) : null}
                      <dt>事件序号</dt>
                      <dd className="num">{entry.seq}</dd>
                      <dt>来源</dt>
                      <dd>{actorLabel(entry.raw.actor_kind)}</dd>
                      <dt>时间</dt>
                      <dd>{new Date(entry.ts).toLocaleString("zh-CN", { hour12: false })}</dd>
                    </dl>
                    <pre className="payload-json">{prettyPayload(detail.payload)}</pre>
                  </details>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}