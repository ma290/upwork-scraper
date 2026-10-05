"use client";

import { useEffect, useState } from "react";
import {
  signInWithPopup,
  signOut,
  onAuthStateChanged,
  User,
} from "firebase/auth";
import { auth, googleProvider } from "@/lib/firebase";

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
  const [keywords, setKeywords] = useState<string[]>(["python", "automation", "web3"]);
  const [newKeyword, setNewKeyword] = useState("");
  const [savingKeywords, setSavingKeywords] = useState(false);
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
          name: currentUser.displayName,
          keywords: keywords,
        }),
      });
      if (res.ok) {
        const data = await res.json();
        setProfile({
          user_id: data.user_id,
          email: currentUser.email || "",
          name: currentUser.displayName || "",
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

  const handleGoogleLogin = async () => {
    try {
      await signInWithPopup(auth, googleProvider);
    } catch (err) {
      console.error("Google sign in error:", err);
    }
  };

  const handleLogout = async () => {
    await signOut(auth);
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
          name: user.displayName,
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
                  {user.displayName || user.email}
                </span>
                <button
                  onClick={handleLogout}
                  className="text-xs bg-slate-800 hover:bg-slate-700 px-3 py-1.5 rounded-lg border border-slate-700 transition"
                >
                  Sign Out
                </button>
              </div>
            ) : (
              <button
                onClick={handleGoogleLogin}
                className="flex items-center gap-2 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold px-4 py-2 rounded-xl text-sm transition shadow-lg shadow-emerald-500/20"
              >
                <span>Sign in with Google</span>
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Hero / Logged-out State */}
      {!user ? (
        <section className="max-w-4xl mx-auto px-4 py-24 text-center">
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 text-emerald-400 text-xs mb-6">
            <span className="animate-pulse">●</span> Real-time Telegram Notifications
          </div>
          <h1 className="text-4xl sm:text-6xl font-extrabold tracking-tight mb-6">
            Get High-Paying Upwork Jobs Delivered to Your{" "}
            <span className="bg-gradient-to-r from-emerald-400 to-cyan-400 bg-clip-text text-transparent">
              Telegram Instantly
            </span>
          </h1>
          <p className="text-slate-400 text-lg sm:text-xl max-w-2xl mx-auto mb-10">
            Be the first to send proposals. Scrapes new jobs 24/7 and alerts your Telegram bot the second a matching client posts.
          </p>
          <button
            onClick={handleGoogleLogin}
            className="inline-flex items-center gap-3 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold px-8 py-4 rounded-2xl text-lg transition shadow-xl shadow-emerald-500/30 hover:scale-105 transform"
          >
            <span>🚀 Sign in with Google to Start Alerts</span>
          </button>
        </section>
      ) : (
        /* Dashboard */
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
