"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, login } from "@/lib/api";

const DEMO_ACCOUNTS = [
  {
    label: "Patient demo",
    email: "patient@agentcare-demo.com",
    password: "demo1234",
  },
  {
    label: "Staff / admin demo",
    email: "staff@agentcare-demo.com",
    password: "demo1234",
  },
] as const;

export default function LoginPage() {
  // useSearchParams needs a Suspense boundary during static export/prerender
  // (Next.js bails a page to client-only render otherwise); the form itself
  // still hydrates and works exactly the same.
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    try {
      const user = await login({ email, password });
      toast.success(`Welcome back, ${user.name}`);
      const next = searchParams.get("next");
      router.push(next ?? (user.role === "staff" ? "/staff" : "/portal"));
      router.refresh();
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Could not reach the server";
      toast.error(message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-full flex-1 items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-lg">Sign in to AgentCare</CardTitle>
          <CardDescription>Demo access for patients and staff/admin.</CardDescription>
        </CardHeader>
        <form onSubmit={handleSubmit}>
          <CardContent className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="rounded-lg border border-dashed p-3" aria-label="Demo accounts">
              <p className="text-xs font-medium text-muted-foreground">Demo accounts</p>
              <div className="mt-2 flex flex-col gap-2">
                {DEMO_ACCOUNTS.map((account) => (
                  <div
                    key={account.email}
                    className="flex items-center justify-between gap-3 text-xs"
                  >
                    <div>
                      <p className="font-medium text-foreground">{account.label}</p>
                      <p className="text-muted-foreground">
                        {account.email} / {account.password}
                      </p>
                    </div>
                    <Button
                      type="button"
                      size="xs"
                      variant="outline"
                      onClick={() => {
                        setEmail(account.email);
                        setPassword(account.password);
                      }}
                    >
                      Use
                    </Button>
                  </div>
                ))}
              </div>
            </div>
          </CardContent>
          <CardFooter className="flex flex-col items-stretch gap-3">
            <Button type="submit" disabled={submitting}>
              {submitting ? "Signing in..." : "Sign in"}
            </Button>
            <p className="text-center text-sm text-muted-foreground">
              No account yet?{" "}
              <a href="/register" className="font-medium text-foreground underline underline-offset-4">
                Register
              </a>
            </p>
          </CardFooter>
        </form>
      </Card>
    </main>
  );
}
