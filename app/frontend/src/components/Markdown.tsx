import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// No rehype-raw: raw HTML in SKILL.md is never rendered, so skill text cannot inject markup or scripts.
// Images are not rendered either: a remote image in skill text could act as a tracking beacon.
// Links in skill text are untrusted: they open in a new tab, with no opener and no referrer.
const components: Components = {
  img: () => null,
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer nofollow" />,
};

export function Markdown({ source }: { source: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{source}</ReactMarkdown>;
}
