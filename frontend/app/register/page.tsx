"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ApiError, login, register } from "@/lib/api";

export default function RegisterPage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [dob, setDob] = useState("");
  const [phone, setPhone] = useState("");
  const [preferredLanguage, setPreferredLanguage] = useState("en");
  const [emergencyContact, setEmergencyContact] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    try {
      await register({
        name,
        email,
        password,
        dob: dob || null,
        phone: phone || null,
        preferred_language: preferredLanguage,
        emergency_contact: emergencyContact || null,
      });
      // Registration only creates the account - it does not set the auth
      // cookie (see backend/app/api/routes_auth.py::register). Log in with
      // the same credentials right away so the new patient lands signed in.
      const user = await login({ email, password });
      toast.success(`Account created. Welcome, ${user.name}`);
      router.push("/portal");
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
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-lg">Create your AgentCare account</CardTitle>
          <CardDescription>Patient self-registration. Staff accounts are provisioned separately.</CardDescription>
        </CardHeader>
        <form onSubmit={handleSubmit}>
          <CardContent className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="name">Full name</Label>
              <Input id="name" required value={name} onChange={(e) => setName(e.target.value)} />
            </div>
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
                autoComplete="new-password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="dob">Date of birth</Label>
                <Input id="dob" type="date" value={dob} onChange={(e) => setDob(e.target.value)} />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="phone">Phone</Label>
                <Input id="phone" type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} />
              </div>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="preferred_language">Preferred language</Label>
              <Select value={preferredLanguage} onValueChange={setPreferredLanguage}>
                <SelectTrigger id="preferred_language" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="en">English</SelectItem>
                  <SelectItem value="de">German</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="emergency_contact">Emergency contact</Label>
              <Input
                id="emergency_contact"
                placeholder="Name and phone number"
                value={emergencyContact}
                onChange={(e) => setEmergencyContact(e.target.value)}
              />
            </div>
          </CardContent>
          <CardFooter className="flex flex-col items-stretch gap-3">
            <Button type="submit" disabled={submitting}>
              {submitting ? "Creating account..." : "Create account"}
            </Button>
            <p className="text-center text-sm text-muted-foreground">
              Already registered?{" "}
              <a href="/login" className="font-medium text-foreground underline underline-offset-4">
                Sign in
              </a>
            </p>
          </CardFooter>
        </form>
      </Card>
    </main>
  );
}
