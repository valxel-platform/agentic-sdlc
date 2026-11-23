import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SignIn } from "./SignIn";

// RTL does not auto-cleanup under vitest with globals: false.
afterEach(cleanup);

// Async waits and the test timeout get room: this VM is shared with other
// agents running the full suite at the same time.
configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 30000 });

const apiMock = vi.hoisted(() => ({
  authConfig: vi.fn(),
  devLogin: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

const githubConfig = {
  mode: "github",
  providers: [
    { id: "github", label: "Sign in with GitHub" },
    { id: "google", label: "Sign in with Google" },
  ],
};

const cognitoConfig = {
  mode: "cognito",
  providers: [
    { id: "email", label: "Sign in with email" },
    { id: "google", label: "Sign in with Google" },
  ],
};

const devConfig = { mode: "dev", teams: ["team-a", "team-b"] };

function setPath(path: string) {
  window.history.pushState({}, "", path);
}

beforeEach(() => {
  vi.clearAllMocks();
  setPath("/");
  apiMock.authConfig.mockResolvedValue(githubConfig);
  apiMock.devLogin.mockResolvedValue({ login: "ada" });
});

describe("loading and errors", () => {
  it("shows the loading line while authConfig is pending", () => {
    apiMock.authConfig.mockReturnValue(new Promise(() => {}));
    render(<SignIn onSignedIn={() => {}} />);
    expect(screen.getByText("Checking how to sign in…")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows an alert when authConfig fails", async () => {
    apiMock.authConfig.mockRejectedValue(new Error("Cannot reach auth"));
    render(<SignIn onSignedIn={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach auth");
    expect(screen.queryByText("Checking how to sign in…")).not.toBeInTheDocument();
  });
});

describe("provider sign-in (non-dev)", () => {
  it("renders one button per provider, first primary, rest secondary", async () => {
    render(<SignIn onSignedIn={() => {}} />);
    const github = await screen.findByRole("link", { name: "Sign in with GitHub" });
    const google = screen.getByRole("link", { name: "Sign in with Google" });
    expect(github).toHaveClass("btn");
    expect(google).toHaveClass("btn", "secondary");
  });

  it("links each provider to /api/auth/login with the encoded return path", async () => {
    setPath("/board?product=CLN");
    render(<SignIn onSignedIn={() => {}} />);
    const github = await screen.findByRole("link", { name: "Sign in with GitHub" });
    expect(github).toHaveAttribute("href", "/api/auth/login?provider=github&next=%2Fboard%3Fproduct%3DCLN");
    expect(screen.getByRole("link", { name: "Sign in with Google" })).toHaveAttribute(
      "href",
      "/api/auth/login?provider=google&next=%2Fboard%3Fproduct%3DCLN",
    );
  });

  it("shows the privacy note", async () => {
    render(<SignIn onSignedIn={() => {}} />);
    await screen.findByRole("link", { name: "Sign in with GitHub" });
    expect(screen.getByText(/Valxel never sees your password/)).toBeInTheDocument();
  });

  it("shows no passkey link when no email provider exists", async () => {
    render(<SignIn onSignedIn={() => {}} />);
    await screen.findByRole("link", { name: "Sign in with GitHub" });
    expect(screen.queryByRole("link", { name: "Use a passkey" })).not.toBeInTheDocument();
  });

  it("shows the passkey link only when an email provider exists", async () => {
    apiMock.authConfig.mockResolvedValue(cognitoConfig);
    setPath("/inbox");
    render(<SignIn onSignedIn={() => {}} />);
    const passkey = await screen.findByRole("link", { name: "Use a passkey" });
    expect(passkey).toHaveAttribute("href", "/api/auth/login?provider=passkey&next=%2Finbox");
  });

  it("offers the team's own identity provider, beside GitHub or alone", async () => {
    apiMock.authConfig.mockResolvedValue({
      mode: "github",
      providers: [
        { id: "github", label: "Sign in with GitHub" },
        { id: "oidc", label: "Sign in with Okta" },
      ],
    });
    setPath("/inbox");
    const { unmount } = render(<SignIn onSignedIn={() => {}} />);
    const okta = await screen.findByRole("link", { name: "Sign in with Okta" });
    expect(okta).toHaveAttribute("href", "/api/auth/login?provider=oidc&next=%2Finbox");
    expect(okta).toHaveClass("secondary");
    unmount();

    apiMock.authConfig.mockResolvedValue({
      mode: "oidc",
      providers: [{ id: "oidc", label: "Sign in with Okta" }],
    });
    render(<SignIn onSignedIn={() => {}} />);
    expect(await screen.findByRole("link", { name: "Sign in with Okta" })).not.toHaveClass("secondary");
    expect(screen.queryByRole("link", { name: /GitHub/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Use a passkey" })).not.toBeInTheDocument();
    expect(screen.getByText(/your company sign-in with multi-factor/)).toBeInTheDocument();
  });

  it("shows the failure message for ?signin=failed", async () => {
    setPath("/?signin=failed");
    render(<SignIn onSignedIn={() => {}} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Sign-in was cancelled or failed. Try again.");
    await screen.findByRole("link", { name: "Sign in with GitHub" });
    expect(screen.getByRole("alert")).toHaveTextContent("Sign-in was cancelled or failed. Try again.");
  });

  it("shows no failure message without ?signin=failed", async () => {
    render(<SignIn onSignedIn={() => {}} />);
    await screen.findByRole("link", { name: "Sign in with GitHub" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders no provider buttons when the provider list is empty", async () => {
    apiMock.authConfig.mockResolvedValue({ mode: "cognito", providers: [] });
    render(<SignIn onSignedIn={() => {}} />);
    await screen.findByText(/Valxel never sees your password/);
    expect(screen.queryByRole("link", { name: /Sign in with/ })).not.toBeInTheDocument();
  });

  it("renders no provider buttons when the config has no providers field", async () => {
    apiMock.authConfig.mockResolvedValue({ mode: "cognito" });
    render(<SignIn onSignedIn={() => {}} />);
    await screen.findByText(/Valxel never sees your password/);
    expect(screen.queryByRole("link", { name: /Sign in with/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Use a passkey" })).not.toBeInTheDocument();
  });
});

describe("dev sign-in", () => {
  it("shows the dev notice and team checkboxes", async () => {
    apiMock.authConfig.mockResolvedValue(devConfig);
    render(<SignIn onSignedIn={() => {}} />);
    expect(
      await screen.findByText("Development sign-in: choose any identity. Not for production."),
    ).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "team-a" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "team-b" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Sign in with/ })).not.toBeInTheDocument();
  });

  it("disables submit until a login is typed", async () => {
    const user = userEvent.setup();
    apiMock.authConfig.mockResolvedValue(devConfig);
    render(<SignIn onSignedIn={() => {}} />);
    const submit = await screen.findByRole("button", { name: "Sign in" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText(/GitHub login/), "   ");
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText(/GitHub login/), "ada");
    expect(submit).toBeEnabled();
  });

  it("submits login, teams and email, then calls onSignedIn", async () => {
    const user = userEvent.setup();
    const onSignedIn = vi.fn();
    apiMock.authConfig.mockResolvedValue(devConfig);
    render(<SignIn onSignedIn={onSignedIn} />);
    await screen.findByText("Development sign-in: choose any identity. Not for production.");
    await user.type(screen.getByLabelText(/GitHub login/), "ada");
    await user.type(screen.getByLabelText(/Email/), "ada@example.com");
    await user.click(screen.getByRole("checkbox", { name: "team-b" }));
    await user.click(screen.getByRole("checkbox", { name: "team-a" }));
    await user.click(screen.getByRole("checkbox", { name: "team-a" }));
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(apiMock.devLogin).toHaveBeenCalledWith("ada", ["team-b"], [], "ada@example.com");
    expect(onSignedIn).toHaveBeenCalledTimes(1);
  });

  it("trims the login and email before submitting", async () => {
    const user = userEvent.setup();
    apiMock.authConfig.mockResolvedValue(devConfig);
    render(<SignIn onSignedIn={() => {}} />);
    await screen.findByText("Development sign-in: choose any identity. Not for production.");
    await user.type(screen.getByLabelText(/GitHub login/), " ada ");
    await user.type(screen.getByLabelText(/Email/), " ada@example.com ");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(apiMock.devLogin).toHaveBeenCalledWith("ada", [], [], "ada@example.com");
  });

  it("shows an alert when devLogin fails and does not call onSignedIn", async () => {
    const user = userEvent.setup();
    const onSignedIn = vi.fn();
    apiMock.authConfig.mockResolvedValue(devConfig);
    apiMock.devLogin.mockRejectedValue(new Error("No such identity"));
    render(<SignIn onSignedIn={onSignedIn} />);
    await screen.findByText("Development sign-in: choose any identity. Not for production.");
    await user.type(screen.getByLabelText(/GitHub login/), "ada");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No such identity");
    expect(onSignedIn).not.toHaveBeenCalled();
  });

  it("renders no team checkboxes when the config has no teams", async () => {
    apiMock.authConfig.mockResolvedValue({ mode: "dev" });
    render(<SignIn onSignedIn={() => {}} />);
    await screen.findByText("Development sign-in: choose any identity. Not for production.");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
});
