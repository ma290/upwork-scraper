"use client";

import { useEffect, useState } from "react";
import {
  createUserWithEmailAndPassword,
  signInWithEmailAndPassword,
  signOut,
  onAuthStateChanged,
  User,
} from "firebase/auth";
import { auth } from "@/lib/firebase";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface UserProfile {
  user_id: string;
  email: string;
  name: string;
  telegram_connected: boolean;
  telegram_chat_id: string | null;
  telegram_link: string | null;
  keywords: string[];
  total_alerts_received: number;
}

interface Job {
  id: string;
  title: string;
  description: string;
  url: string;
  amount: string | null;
  hourly_rate: string | null;
  job_type: string;
  skills: string[];
  client_location: string;
  client_payment_verified: number;
  client_rating: number | null;
  found_at: string;
  keyword: string;
}

export default function Home() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [profile, setProfile] = useState<UserProfile | null>(null);

  // Email & Password Auth State
  const [isSignUp, setIsSignUp] = useState(false);
  const [emailInput, setEmailInput] = useState("");
  const [passwordInput, setPasswordInput] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [authLoading, setAuthLoading] = useState(false);

  // Keywords State
  const [keywords, setKeywords] = useState<string[]>(["python", "automation", "web3"]);
  const [newKeyword, setNewKeyword] = useState("");
  const [savingKeywords, setSavingKeywords] = useState(false);

  // Jobs State
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loadingJobs, setLoadingJobs] = useState(false);

  // Listen to Firebase Auth state
  useEffect(() => {
    const unsubscribe = onAuthStateChanged(auth, async (currentUser) => {
      setUser(currentUser);
      if (currentUser) {
        await syncUserWithBackend(currentUser);
      } else {
        setProfile(null);
      }
      setLoading(false);
    });
    return () => unsubscribe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch recent jobs
  useEffect(() => {
    fetchRecentJobs();
  }, []);

  // Periodically refresh user profile to detect when Telegram bot is started
  useEffect(() => {
    if (!user || profile?.telegram_connected) return;

    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/users/${user.uid}`);
        if (res.ok) {
          const data: UserProfile = await res.json();
          if (data.telegram_connected) {
            setProfile(data);
          }
        }
      } catch (err) {
        console.error("Error polling profile status:", err);
      }
    }, 4000);

    return () => clearInterval(interval);
  }, [user, profile?.telegram_connected]);

  const syncUserWithBackend = async (currentUser: User) => {
    try {
      const res = await fetch(`${API_BASE}/api/users/sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: currentUser.uid,
          email: currentUser.email,
          name: currentUser.email?.split("@")[0] || "User",
          keywords: keywords,
        }),
      });
      if (res.ok) {
        const data = await res.json();
        setProfile({
          user_id: data.user_id,
          email: currentUser.email || "",
          name: currentUser.email?.split("@")[0] || "User",
          telegram_connected: data.telegram_connected,
          telegram_chat_id: data.telegram_chat_id,
          telegram_link: data.telegram_link,
          keywords: data.keywords || keywords,
          total_alerts_received: 0,
        });
        if (data.keywords && data.keywords.length > 0) {
          setKeywords(data.keywords);
        }
      }
    } catch (err) {
      console.error("Failed to sync user with backend:", err);
    }
  };

  const handleEmailAuth = async (e: React.FormEvent) => {
    e.preventDefault();
    setAuthError(null);
    setAuthLoading(true);

    try {
      if (isSignUp) {
        await createUserWithEmailAndPassword(auth, emailInput.trim(), passwordInput);
      } else {
        await signInWithEmailAndPassword(auth, emailInput.trim(), passwordInput);
      }
    } catch (err: unknown) {
      console.error("Auth error:", err);
      const message = err instanceof Error ? err.message : "Authentication failed";
      if (message.includes("auth/email-already-in-use")) {
        setAuthError("This email is already registered. Please log in.");
      } else if (message.includes("auth/invalid-credential") || message.includes("auth/wrong-password") || message.includes("auth/user-not-found")) {
        setAuthError("Invalid email or password.");
      } else if (message.includes("auth/weak-password")) {
        setAuthError("Password should be at least 6 characters.");
      } else if (message.includes("auth/invalid-email")) {
        setAuthError("Please enter a valid email address.");
      } else {
        setAuthError(message);
      }
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogout = async () => {
    await signOut(auth);
  };

  const fetchRecentJobs = async () => {
    setLoadingJobs(true);
    try {
      const res = await fetch(`${API_BASE}/api/jobs?limit=15`);
      if (res.ok) {
        const data = await res.json();
        setJobs(data.jobs || []);
      }
    } catch (err) {
      console.error("Failed to fetch jobs:", err);
    } finally {
      setLoadingJobs(false);
    }
  };

  const addKeyword = () => {
    const trimmed = newKeyword.trim().toLowerCase();
    if (trimmed && !keywords.includes(trimmed)) {
      setKeywords([...keywords, trimmed]);
      setNewKeyword("");
    }
  };

  const removeKeyword = (kw: string) => {
    setKeywords(keywords.filter((k) => k !== kw));
  };

  const saveKeywordPreferences = async () => {
    if (!user) return;
    setSavingKeywords(true);
    try {
      await fetch(`${API_BASE}/api/users/sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: user.uid,
          email: user.email,
          name: user.email?.split("@")[0] || "User",
          keywords: keywords,
        }),
      });
      alert("✅ Keywords saved! Your Telegram bot will now watch for these terms.");
    } catch (err) {
      alert("Failed to save keywords: " + err);
    } finally {
      setSavingKeywords(false);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center text-white">
        <div className="animate-spin rounded-full h-10 w-10 border-t-2 border-emerald-400"></div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 selection:bg-emerald-500 selection:text-white">
      {/* Header */}
      <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur sticky top-0 z-50">
        <div className="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-2xl">⚡</span>
            <span className="font-bold text-lg tracking-tight bg-gradient-to-r from-emerald-400 to-teal-200 bg-clip-text text-transparent">
              Upwork Job Radar
            </span>
          </div>
          <div>
            {user ? (
              <div className="flex items-center gap-3">
                <span className="text-sm text-slate-300 hidden sm:inline">
                  {user.email}
                </span>
                <button
                  onClick={handleLogout}
                  className="text-xs bg-slate-800 hover:bg-slate-700 px-3 py-1.5 rounded-lg border border-slate-700 transition"
                >
                  Sign Out
                </button>
              </div>
            ) : null}
          </div>
        </div>
      </header>

      {/* Logged-out State: Email & Password Form */}
      {!user ? (
        <section className="max-w-md mx-auto px-4 py-16">
          <div className="text-center mb-8">
            <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 text-emerald-400 text-xs mb-4">
              <span className="animate-pulse">●</span> Real-time Telegram Notifications
            </div>
            <h1 className="text-3xl font-extrabold tracking-tight mb-2">
              Upwork Job Radar
            </h1>
            <p className="text-slate-400 text-sm">
              Sign in or create an account to start receiving instant job alerts.
            </p>
          </div>

          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 sm:p-8 shadow-2xl">
            {/* Toggle Tabs */}
            <div className="flex border-b border-slate-800 mb-6">
              <button
                type="button"
                onClick={() => { setIsSignUp(false); setAuthError(null); }}
                className={`flex-1 pb-3 text-sm font-semibold transition border-b-2 ${
                  !isSignUp
                    ? "border-emerald-400 text-emerald-400"
                    : "border-transparent text-slate-400 hover:text-slate-200"
                }`}
              >
                Log In
              </button>
              <button
                type="button"
                onClick={() => { setIsSignUp(true); setAuthError(null); }}
                className={`flex-1 pb-3 text-sm font-semibold transition border-b-2 ${
                  isSignUp
                    ? "border-emerald-400 text-emerald-400"
                    : "border-transparent text-slate-400 hover:text-slate-200"
                }`}
              >
                Sign Up
              </button>
            </div>

            {authError && (
              <div className="mb-4 p-3 rounded-xl bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
                {authError}
              </div>
            )}

            <form onSubmit={handleEmailAuth} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-300 mb-1">
                  Email Address
                </label>
                <input
                  type="email"
                  required
                  placeholder="you@example.com"
                  value={emailInput}
                  onChange={(e) => setEmailInput(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-400"
                />
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-300 mb-1">
                  Password
                </label>
                <input
                  type="password"
                  required
                  minLength={6}
                  placeholder="••••••••"
                  value={passwordInput}
                  onChange={(e) => setPasswordInput(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-400"
                />
              </div>

              <button
                type="submit"
                disabled={authLoading}
                className="w-full mt-2 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold py-3 rounded-xl text-sm transition shadow-lg shadow-emerald-500/20 disabled:opacity-50"
              >
                {authLoading ? "Processing..." : isSignUp ? "Create Account" : "Log In"}
              </button>
            </form>
          </div>
        </section>
      ) : (
        /* Dashboard for Logged-In User */
        <main className="max-w-6xl mx-auto px-4 py-8 space-y-8">
          {/* Telegram Connection Banner */}
          <div className="bg-gradient-to-br from-slate-900 to-slate-800/80 border border-slate-800 rounded-2xl p-6 sm:p-8 shadow-xl">
            <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-6">
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <span className="text-2xl">📱</span>
                  <h2 className="text-xl font-bold">Telegram Bot Alerts</h2>
                </div>
                <p className="text-sm text-slate-400">
                  Connect your personal Telegram account so the bot knows where to send your job alerts.
                </p>
                <div className="pt-2">
                  {profile?.telegram_connected ? (
                    <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/40">
                      <span>✅</span> Telegram Connected (Chat ID: {profile.telegram_chat_id})
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-300 border border-amber-500/40">
                      <span>⚠️</span> Telegram Not Connected Yet
                    </span>
                  )}
                </div>
              </div>

              <div>
                {profile?.telegram_link ? (
                  <a
                    href={profile.telegram_link}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-2 bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold px-6 py-3.5 rounded-xl text-sm transition shadow-lg shadow-cyan-500/20"
                  >
                    <span>👉 Click to Open & Start Telegram Bot</span>
                  </a>
                ) : (
                  <div className="text-xs text-slate-400">
                    Set `TELEGRAM_BOT_USERNAME` in backend `.env` to enable 1-click connect link.
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Keywords Configuration */}
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-6 sm:p-8">
            <h2 className="text-xl font-bold mb-2 flex items-center gap-2">
              <span>🎯</span> Your Alert Keywords
            </h2>
            <p className="text-sm text-slate-400 mb-6">
              The scraper will monitor Upwork for these terms and ping your Telegram whenever a new matching job appears.
            </p>

            <div className="flex flex-wrap gap-2 mb-4">
              {keywords.map((kw) => (
                <span
                  key={kw}
                  className="inline-flex items-center gap-1.5 bg-slate-800 border border-slate-700 px-3 py-1 rounded-lg text-sm"
                >
                  <span className="text-emerald-400 font-medium">#{kw}</span>
                  <button
                    onClick={() => removeKeyword(kw)}
                    className="text-slate-400 hover:text-red-400 transition ml-1"
                  >
                    ✕
                  </button>
                </span>
              ))}
            </div>

            <div className="flex flex-col sm:flex-row gap-3">
              <input
                type="text"
                placeholder="Add keyword (e.g. solidity, react, python, scraper)..."
                value={newKeyword}
                onChange={(e) => setNewKeyword(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && addKeyword()}
                className="flex-1 bg-slate-950 border border-slate-700 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-400"
              />
              <button
                onClick={addKeyword}
                className="bg-slate-800 hover:bg-slate-700 px-5 py-2.5 rounded-xl text-sm font-semibold transition"
              >
                + Add
              </button>
              <button
                onClick={saveKeywordPreferences}
                disabled={savingKeywords}
                className="bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold px-6 py-2.5 rounded-xl text-sm transition disabled:opacity-50"
              >
                {savingKeywords ? "Saving..." : "Save Preferences"}
              </button>
            </div>
          </div>

          {/* Live Discovered Jobs Feed */}
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-bold flex items-center gap-2">
                <span>🔥</span> Recent Discovered Jobs
              </h2>
              <button
                onClick={fetchRecentJobs}
                className="text-xs text-slate-400 hover:text-white transition flex items-center gap-1"
              >
                <span>🔄</span> Refresh
              </button>
            </div>

            {loadingJobs ? (
              <div className="p-12 text-center text-slate-500">Loading jobs from Termux API...</div>
            ) : jobs.length === 0 ? (
              <div className="bg-slate-900/40 border border-slate-800 rounded-2xl p-12 text-center text-slate-500">
                No jobs in the database yet. The scraper is running its cycle.
              </div>
            ) : (
              <div className="grid gap-4">
                {jobs.map((job) => (
                  <div
                    key={job.id}
                    className="bg-slate-900/70 border border-slate-800 hover:border-slate-700 p-5 rounded-xl transition space-y-3"
                  >
                    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                      <a
                        href={job.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="font-bold text-base text-emerald-400 hover:underline"
                      >
                        {job.title}
                      </a>
                      <span className="text-xs font-semibold px-2.5 py-1 rounded bg-slate-800 border border-slate-700 text-slate-300 w-fit">
                        {job.amount || job.hourly_rate || "Not specified"}
                      </span>
                    </div>

                    <p className="text-xs text-slate-400 line-clamp-2">{job.description}</p>

                    <div className="flex flex-wrap items-center justify-between text-xs text-slate-500 gap-2 pt-1 border-t border-slate-800/60">
                      <div>
                        <span>🌍 {job.client_location || "Unknown"}</span>
                        <span className="ml-3">
                          {job.client_payment_verified ? "Verified ✅" : "Unverified ⚠️"}
                        </span>
                      </div>
                      <a
                        href={job.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-emerald-400 hover:text-emerald-300 font-semibold"
                      >
                        Apply on Upwork →
                      </a>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </main>
      )}
    </div>
  );
}
