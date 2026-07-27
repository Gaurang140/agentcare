"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ApiError, getProfile, updateProfile } from "@/lib/api";

export default function ProfilePage() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [dateOfBirth, setDateOfBirth] = useState("");
  const [phone, setPhone] = useState("");
  const [preferredLanguage, setPreferredLanguage] = useState<"en" | "de">("en");
  const [emergencyContact, setEmergencyContact] = useState("");

  useEffect(() => {
    let cancelled = false;
    getProfile()
      .then((profile) => {
        if (cancelled) return;
        setName(profile.name);
        setEmail(profile.email);
        setDateOfBirth(profile.date_of_birth ?? "");
        setPhone(profile.phone ?? "");
        setPreferredLanguage(profile.preferred_language === "de" ? "de" : "en");
        setEmergencyContact(profile.emergency_contact ?? "");
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          toast.error(err instanceof ApiError ? err.message : "Could not load your profile");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      const updated = await updateProfile({
        date_of_birth: dateOfBirth || null,
        phone: phone || null,
        preferred_language: preferredLanguage,
        emergency_contact: emergencyContact || null,
      });
      setPreferredLanguage(updated.preferred_language === "de" ? "de" : "en");
      toast.success("Profile updated");
    } catch (err: unknown) {
      toast.error(err instanceof ApiError ? err.message : "Could not save your profile");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="max-w-xl">
      <CardHeader>
        <CardTitle className="text-base">Your profile</CardTitle>
        <CardDescription>
          Contact details and language. Responses and reminders follow your
          preferred language.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="grid gap-1.5">
              <Label htmlFor="name">Name</Label>
              <Input id="name" value={name} disabled />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="email">Email</Label>
              <Input id="email" value={email} disabled />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="date_of_birth">Date of birth</Label>
              <Input
                id="date_of_birth"
                type="date"
                value={dateOfBirth}
                onChange={(e) => setDateOfBirth(e.target.value)}
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="phone">Phone</Label>
              <Input
                id="phone"
                type="tel"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                placeholder="+49 170 1234567"
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="preferred_language">Preferred language</Label>
              <Select
                value={preferredLanguage}
                onValueChange={(value) => setPreferredLanguage(value === "de" ? "de" : "en")}
              >
                <SelectTrigger id="preferred_language" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="en">English</SelectItem>
                  <SelectItem value="de">Deutsch</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="emergency_contact">Emergency contact</Label>
              <Input
                id="emergency_contact"
                value={emergencyContact}
                onChange={(e) => setEmergencyContact(e.target.value)}
                placeholder="Name and phone number"
              />
            </div>
            <div>
              <Button type="submit" disabled={saving}>
                {saving ? "Saving..." : "Save changes"}
              </Button>
            </div>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
