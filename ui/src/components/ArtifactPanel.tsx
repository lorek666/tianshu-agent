import { useEffect, useState } from "react";
import { humanizeHostError } from "../api/errors";
import type { HostClient } from "../api/host";

interface Artifact {
  artifact_id: string;
  path: string;
  bytes: number;
  sha256: string;
}

interface ArtifactPreview extends Artifact {
  text: string;
  truncated: boolean;
}

interface ArtifactPanelProps {
  host: HostClient;
  runId: string;
  revision: number;
}

export function ArtifactPanel(props: ArtifactPanelProps) {
  return <RunArtifacts key={props.runId} {...props} />;
}

function RunArtifacts({ host, runId, revision }: ArtifactPanelProps) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Artifact[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let current = true;
    setListError(null);
    void host.rpc<{ artifacts: Artifact[]; truncated?: boolean }>("artifact.list", { run_id: runId }).then(
      ({ artifacts, truncated }) => {
        if (!current) return;
        setItems(artifacts);
        setTruncated(truncated === true);
      },
      (error: unknown) => { if (current) setListError(humanizeHostError(error)); },
    );
    return () => { current = false; };
  }, [host, runId, revision, open]);

  const showPreview = async (id: string): Promise<void> => {
    setPreview(null);
    setPreviewError(null);
    setLoadingPreview(true);
    try {
      const result = await host.rpc<{ artifact: ArtifactPreview }>("artifact.preview", {
        run_id: runId,
        artifact_id: id,
      });
      setPreview(result.artifact);
    } catch (error) {
      setPreviewError(humanizeHostError(error));
    } finally {
      setLoadingPreview(false);
    }
  };

  return (
    <section className="stream-timeline-card" aria-label="本次运行的文件产物">
      <button
        type="button"
        className="chip-btn"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >{open ? "收起文件产物" : "查看文件产物"}</button>
      {open ? (
        <div>
          {listError !== null ? <p className="form-error" role="alert">{listError}</p> : null}
          {items === null && listError === null ? <p role="status">正在读取产物…</p> : null}
          {items?.length === 0 ? <p className="subtle">本次运行没有可预览的文本产物。</p> : null}
          {items && items.length > 0 ? (
            <ul>
              {items.map((item) => (
                <li key={item.artifact_id}>
                  <button
                    type="button"
                    className="link-btn"
                    disabled={loadingPreview}
                    onClick={() => void showPreview(item.artifact_id)}
                  >{item.path}</button>
                  <span className="subtle"> · {item.bytes.toLocaleString()} 字节</span>
                </li>
              ))}
            </ul>
          ) : null}
          {truncated ? <p className="subtle">产物过多，仅显示部分记录。</p> : null}
          {loadingPreview ? <p role="status">正在读取预览…</p> : null}
          {previewError !== null ? <p className="form-error" role="alert">{previewError}</p> : null}
          {preview !== null ? (
            <figure>
              <figcaption className="section-title">
                {preview.path}（运行时版本{preview.truncated ? "，预览已截断" : ""}）
              </figcaption>
              <pre className="payload-json">{preview.text}</pre>
            </figure>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
