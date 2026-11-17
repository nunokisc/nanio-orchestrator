import { useQuery } from "@tanstack/react-query";
import { Button, CodeBlock, Modal, Notice, QueryView } from "./ui";

/** The config the orchestrator generates for a pool or a vhost, as it would be written. */
export function PreviewDialog({
  title,
  queryKey,
  load,
  onClose,
}: {
  title: string;
  queryKey: readonly unknown[];
  load: () => Promise<{ filepath: string; content: string | null }>;
  onClose: () => void;
}) {
  const query = useQuery({ queryKey: ["preview", ...queryKey], queryFn: load, staleTime: 0 });
  return (
    <Modal title={title} onClose={onClose} size="lg" footer={<Button onClick={onClose}>Close</Button>}>
      <QueryView query={query}>
        {(preview) =>
          preview.content === null ? (
            <Notice tone="info" title="No file">
              A pool without members has no upstream block: nginx refuses an empty one, so nothing is written to{" "}
              <span className="mono">{preview.filepath}</span>.
            </Notice>
          ) : (
            <CodeBlock title={preview.filepath} code={preview.content} />
          )
        }
      </QueryView>
    </Modal>
  );
}
