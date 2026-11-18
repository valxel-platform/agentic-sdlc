import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Attachment } from "../api";
import { AttachmentList, size } from "./Attachments";

describe("size", () => {
  it.each([
    [0, "0 B"],
    [512, "512 B"],
    [1023, "1023 B"],
    [1024, "1 KB"],
    [2048, "2 KB"],
    [1536, "2 KB"],
    [1024 * 1024, "1.0 MB"],
    [2.5 * 1024 * 1024, "2.5 MB"],
  ])("formats %s as %s", (n, expected) => {
    expect(size(n)).toBe(expected);
  });
});

function attachment(over: Partial<Attachment> = {}): Attachment {
  return { id: "a1", name: "shot.png", kind: "image", size: 2048, read_by_agents: false, ...over };
}

describe("AttachmentList", () => {
  it("renders nothing for no attachments", () => {
    const { container } = render(<AttachmentList items={[]} href={(a) => `/api/x/${a.id}`} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders each attachment with kind, size and who reads it", () => {
    render(
      <AttachmentList
        items={[
          attachment(),
          attachment({ id: "a2", name: "notes.txt", kind: "text", size: 120, read_by_agents: true }),
          attachment({
            id: "a3",
            name: "spec.pdf",
            kind: "pdf",
            size: 3 * 1024 * 1024,
            read_by_agents: false,
          }),
        ]}
        href={(a) => `/api/intake/sess-1/attachments/${a.id}`}
      />,
    );
    const shot = screen.getByRole("link", { name: "shot.png" });
    expect(shot).toHaveAttribute("href", "/api/intake/sess-1/attachments/a1");
    expect(shot).toHaveAttribute("download", "shot.png");
    expect(screen.getByText(/Image, 2 KB\. For people; agents see only its name\./)).toBeInTheDocument();
    expect(screen.getByText(/Text, 120 B\. Agents read it at discovery and spec\./)).toBeInTheDocument();
    expect(screen.getByText(/PDF, 3\.0 MB\./)).toBeInTheDocument();
    expect(screen.getByText("IMG")).toBeInTheDocument();
    expect(screen.getByText("TXT")).toBeInTheDocument();
    expect(screen.getByText("PDF")).toBeInTheDocument();
  });

  it("calls onRemove with the attachment", async () => {
    const user = userEvent.setup();
    const onRemove = vi.fn();
    render(<AttachmentList items={[attachment()]} href={(a) => `/api/x/${a.id}`} onRemove={onRemove} />);
    await user.click(screen.getByRole("button", { name: "Remove shot.png" }));
    expect(onRemove).toHaveBeenCalledWith(expect.objectContaining({ id: "a1" }));
  });

  it("shows no remove button without onRemove", () => {
    render(<AttachmentList items={[attachment()]} href={(a) => `/api/x/${a.id}`} />);
    expect(screen.queryByRole("button", { name: /Remove/ })).not.toBeInTheDocument();
  });

  it("falls back to the raw kind for unknown kinds", () => {
    render(
      <AttachmentList
        items={[attachment({ kind: "other" as Attachment["kind"] })]}
        href={(a) => `/api/x/${a.id}`}
      />,
    );
    expect(screen.getByText(/other, 2 KB/)).toBeInTheDocument();
  });
});
