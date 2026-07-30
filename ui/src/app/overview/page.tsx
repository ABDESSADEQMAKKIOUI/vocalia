"use client";

import Link from 'next/link';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { useAuth } from '@/lib/auth';
import { DOCS_URL, SUPPORT_EMAIL, SUPPORT_MAILTO } from '@/lib/support';

export default function OverviewPage() {
    const { user, provider } = useAuth();
    const isOSSMode = provider !== 'stack';

    return (
        <div className="container mx-auto px-4 py-8">
            <div className="max-w-4xl mx-auto">
                {/* Welcome Card */}
                <Card className="mb-8">
                    <CardHeader>
                        <CardTitle className="text-3xl">
                            {isOSSMode ? (
                                "Welcome to Volira"
                            ) : (
                                `Welcome${user?.displayName ? `, ${user.displayName.split(' ')[0]}` : ''}!`
                            )}
                        </CardTitle>
                        <CardDescription className="text-lg mt-2">
                            {/* Was an open-source pitch asking for a GitHub star.
                                It described the upstream project, not this
                                product, and the badge it referred to is gone. */}
                            Build and run AI agents for voice, WhatsApp and the web.
                        </CardDescription>
                    </CardHeader>
                    {/* The upstream "Star us on GitHub" badge pointed at the
                        project this platform is built on. Removed: a customer of
                        this deployment has no business being sent upstream. */}
                    <CardContent />

                </Card>

                {/* Quick Actions */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <Card>
                        <CardHeader>
                            <CardTitle>Create and Manage your Voice Agents</CardTitle>
                            <CardDescription>
                                Build powerful AI Voice Agents with our visual editor
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Button asChild>
                                <Link href="/workflow">
                                    Go to Agents
                                </Link>
                            </Button>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle>Configure Services</CardTitle>
                            <CardDescription>
                                Set up your AI services like LLM, TTS, and STT providers
                            </CardDescription>
                        </CardHeader>
                        <CardContent>
                            <Button asChild variant="outline">
                                <Link href="/model-configurations">
                                    Configure Models
                                </Link>
                            </Button>
                        </CardContent>
                    </Card>
                </div>

                {/* Resources Section */}
                <Card className="mt-8">
                    <CardHeader>
                        <CardTitle>Resources</CardTitle>
                        <CardDescription>
                            Get help and learn more about Volira
                        </CardDescription>
                    </CardHeader>
                    <CardContent>
                        <div className="flex flex-wrap gap-4">
                            {/* No documentation site is configured yet, so point people
                                at a human instead of at a dead link. */}
                            {DOCS_URL ? (
                                <Button asChild variant="outline">
                                    <a
                                        href={DOCS_URL}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                    >
                                        Documentation
                                    </a>
                                </Button>
                            ) : (
                                <Button asChild variant="outline">
                                    <a href={SUPPORT_MAILTO}>
                                        Email support ({SUPPORT_EMAIL})
                                    </a>
                                </Button>
                            )}
                            {/* "Report an Issue" used to open the upstream issue
                                tracker. A customer's bug report belongs to this
                                platform's own support, not to a public repo. */}
                        </div>
                    </CardContent>
                </Card>
            </div>
        </div>
    );
}
