"use client";

import { AlertTriangle, CalendarDays, ExternalLink, FileText, Folder, HardDrive, Info, Lock, Mail, RefreshCw, Sheet as SheetIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type {
    WorkspaceOverview,
    WorkspaceSectionFlags,
} from "@/lib/googleWorkspace";

import { familyLabel, formatTimestamp, scopeLabel } from "./scopeLabels";

interface WorkspaceResourcesProps {
    overview: WorkspaceOverview | null;
    loading: boolean;
    error?: string | null;
    /** Send the user back through the consent screen to widen the grant. */
    onReconnect: () => void;
    onRetry: () => void;
}

/**
 * An empty section is never shown as "nothing here" without saying why.
 *
 * This is the whole point of the section flags: a customer who granted Sheets
 * but not Gmail, looking at an empty Gmail tab, concludes their mail vanished —
 * or that the integration is broken — when the truth is that they never gave
 * that permission and one click fixes it. So a scope gap names the missing
 * permissions and offers the fix, a failed call says Google failed, and only a
 * genuinely empty account gets the neutral message.
 */
function SectionBody({
    section,
    family,
    itemCount,
    emptyLabel,
    requestedScopes,
    onReconnect,
    onRetry,
    children,
}: {
    section: WorkspaceSectionFlags;
    family: string;
    itemCount: number;
    emptyLabel: string;
    /**
     * Scopes this deployment asks for on the consent screen. A permission the
     * deployment never requests cannot be granted by consenting again, however
     * many times the owner clicks — only an operator widening
     * GOOGLE_OAUTH_SCOPES can add it.
     */
    requestedScopes: string[];
    onReconnect: () => void;
    onRetry: () => void;
    children: ReactNode;
}) {
    if (section.scope_missing) {
        // A broad scope covers narrower ones, so this is a prefix-aware test
        // rather than set membership: a deployment requesting "drive" does
        // request "drive.readonly".
        const requestable = section.missing_scopes.every((missing) =>
            requestedScopes.some(
                (requested) =>
                    requested === missing ||
                    (missing.startsWith(`${requested}.`) && requested.includes("/auth/")),
            ),
        );
        return (
            <div className="rounded-lg border border-border bg-muted/40 p-4">
                <div className="flex gap-3">
                    <Lock className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                    <div className="space-y-2">
                        <p className="font-medium">
                            {familyLabel(family)} access was not granted
                        </p>
                        <p className="text-sm text-muted-foreground">
                            Nothing is missing from your Google account — this connection
                            simply does not have permission to look. Reconnect and accept the
                            following on Google&apos;s consent screen:
                        </p>
                        {section.missing_scopes.length > 0 && (
                            <div className="flex flex-wrap gap-1.5">
                                {section.missing_scopes.map((scope) => (
                                    <Badge key={scope} variant="outline" title={scope}>
                                        {scopeLabel(scope)}
                                    </Badge>
                                ))}
                            </div>
                        )}
                        {requestable ? (
                            <Button size="sm" variant="outline" onClick={onReconnect}>
                                Grant {familyLabel(family)} access
                            </Button>
                        ) : (
                            <p className="text-sm text-muted-foreground">
                                This Volira deployment does not ask for this permission, so
                                reconnecting will not add it. An administrator has to enable
                                it first.
                            </p>
                        )}
                    </div>
                </div>
            </div>
        );
    }

    if (section.error) {
        return (
            <div className="rounded-lg border border-destructive/20 bg-destructive/10 p-4">
                <div className="flex gap-3">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                    <div className="space-y-2">
                        <p className="font-medium text-destructive">
                            {familyLabel(family)} could not be loaded
                        </p>
                        <p className="text-sm text-destructive/90">{section.error}</p>
                        <Button size="sm" variant="outline" onClick={onRetry}>
                            <RefreshCw className="h-4 w-4" />
                            Try again
                        </Button>
                    </div>
                </div>
            </div>
        );
    }

    // Shown whether or not there are items: with a narrow grant a SHORT list is
    // just as misleading as an empty one.
    const notice = section.notice ? (
        <div className="mb-3 flex gap-2 rounded-lg border border-border bg-muted/40 p-3">
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
            <p className="text-sm text-muted-foreground">{section.notice}</p>
        </div>
    ) : null;

    if (itemCount === 0) {
        return (
            <>
                {notice}
                <p className="py-6 text-sm text-muted-foreground">{emptyLabel}</p>
            </>
        );
    }

    return (
        <>
            {notice}
            {children}
        </>
    );
}

/** One resource line: what it is, when it changed, and a link out to Google. */
function ResourceRow({
    icon,
    name,
    meta,
    href,
}: {
    icon: ReactNode;
    name: string;
    meta?: string | null;
    href?: string | null;
}) {
    return (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-border p-3">
            <div className="flex min-w-0 items-center gap-3">
                <span className="text-muted-foreground">{icon}</span>
                <div className="min-w-0">
                    <p className="truncate font-medium">{name}</p>
                    {meta && <p className="truncate text-xs text-muted-foreground">{meta}</p>}
                </div>
            </div>
            {href && (
                <Button variant="ghost" size="sm" asChild>
                    <a href={href} target="_blank" rel="noopener noreferrer">
                        <ExternalLink className="h-4 w-4" />
                        <span className="sr-only">Open in Google</span>
                    </a>
                </Button>
            )}
        </div>
    );
}

/** Tab label: a count when the section works, a lock when the grant is too narrow. */
function TabLabel({
    icon,
    label,
    section,
    count,
}: {
    icon: ReactNode;
    label: string;
    section: WorkspaceSectionFlags;
    count: number;
}) {
    return (
        <span className="flex items-center gap-1.5">
            {icon}
            <span>{label}</span>
            {section.scope_missing ? (
                <Lock className="h-3 w-3 text-muted-foreground" />
            ) : section.error ? (
                <AlertTriangle className="h-3 w-3 text-destructive" />
            ) : (
                <span className="text-muted-foreground">({count})</span>
            )}
        </span>
    );
}

export function WorkspaceResources({
    overview,
    loading,
    error,
    onReconnect,
    onRetry,
}: WorkspaceResourcesProps) {
    // The overview already carries what the deployment asks for, so the panel
    // never has to be told separately — and never drifts from the server.
    const requestedScopes = overview?.requested_scopes ?? [];

    if (loading) {
        return (
            <Card>
                <CardHeader>
                    <CardTitle>Workspace resources</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                    <Skeleton className="h-9 w-full max-w-lg" />
                    {[0, 1, 2].map((row) => (
                        <Skeleton key={row} className="h-14 w-full" />
                    ))}
                </CardContent>
            </Card>
        );
    }

    if (error) {
        return (
            <Card>
                <CardHeader>
                    <CardTitle>Workspace resources</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                    <p className="text-sm text-destructive">{error}</p>
                    <Button variant="outline" size="sm" onClick={onRetry}>
                        <RefreshCw className="h-4 w-4" />
                        Try again
                    </Button>
                </CardContent>
            </Card>
        );
    }

    if (!overview) {
        return null;
    }

    const { sheets, calendars, drive, gmail, docs } = overview;
    const driveCount = drive.root_folders.length + drive.recent_files.length;

    return (
        <Card>
            <CardHeader>
                <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                        <CardTitle>Workspace resources</CardTitle>
                        <CardDescription>
                            What this Google account contains. Names and links only — no file
                            contents are read here.
                        </CardDescription>
                    </div>
                    <Button variant="outline" size="sm" onClick={onRetry}>
                        <RefreshCw className="h-4 w-4" />
                        Refresh
                    </Button>
                </div>
            </CardHeader>

            <CardContent>
                <Tabs defaultValue="sheets" className="gap-4">
                    <div className="overflow-x-auto">
                        <TabsList>
                            <TabsTrigger value="sheets">
                                <TabLabel
                                    icon={<SheetIcon className="h-4 w-4" />}
                                    label="Sheets"
                                    section={sheets}
                                    count={sheets.items.length}
                                />
                            </TabsTrigger>
                            <TabsTrigger value="calendar">
                                <TabLabel
                                    icon={<CalendarDays className="h-4 w-4" />}
                                    label="Calendar"
                                    section={calendars}
                                    count={calendars.items.length}
                                />
                            </TabsTrigger>
                            <TabsTrigger value="drive">
                                <TabLabel
                                    icon={<HardDrive className="h-4 w-4" />}
                                    label="Drive"
                                    section={drive}
                                    count={driveCount}
                                />
                            </TabsTrigger>
                            <TabsTrigger value="gmail">
                                <TabLabel
                                    icon={<Mail className="h-4 w-4" />}
                                    label="Gmail"
                                    section={gmail}
                                    count={gmail.labels.length}
                                />
                            </TabsTrigger>
                            <TabsTrigger value="docs">
                                <TabLabel
                                    icon={<FileText className="h-4 w-4" />}
                                    label="Docs"
                                    section={docs}
                                    count={docs.items.length}
                                />
                            </TabsTrigger>
                        </TabsList>
                    </div>

                    <TabsContent value="sheets">
                        <SectionBody
                            section={sheets}
                            family="sheets"
                            itemCount={sheets.items.length}
                            emptyLabel="This account has no spreadsheets."
                            requestedScopes={requestedScopes}
                            onReconnect={onReconnect}
                            onRetry={onRetry}
                        >
                            <div className="space-y-2">
                                {sheets.items.map((item) => (
                                    <ResourceRow
                                        key={item.id}
                                        icon={<SheetIcon className="h-4 w-4" />}
                                        name={item.name}
                                        meta={
                                            formatTimestamp(item.modified_time)
                                                ? `Modified ${formatTimestamp(item.modified_time)}`
                                                : null
                                        }
                                        href={item.web_url}
                                    />
                                ))}
                            </div>
                        </SectionBody>
                    </TabsContent>

                    <TabsContent value="calendar">
                        <SectionBody
                            section={calendars}
                            family="calendar"
                            itemCount={calendars.items.length}
                            emptyLabel="This account has no calendars."
                            requestedScopes={requestedScopes}
                            onReconnect={onReconnect}
                            onRetry={onRetry}
                        >
                            <div className="space-y-2">
                                {calendars.items.map((calendar) => (
                                    <div
                                        key={calendar.id}
                                        className="flex items-center justify-between gap-3 rounded-lg border border-border p-3"
                                    >
                                        <div className="flex min-w-0 items-center gap-3">
                                            <CalendarDays className="h-4 w-4 text-muted-foreground" />
                                            <div className="min-w-0">
                                                <p className="truncate font-medium">
                                                    {calendar.summary || calendar.id}
                                                </p>
                                                <p className="truncate text-xs text-muted-foreground">
                                                    {[calendar.time_zone, calendar.access_role]
                                                        .filter(Boolean)
                                                        .join(" · ") || calendar.id}
                                                </p>
                                            </div>
                                        </div>
                                        {calendar.primary && <Badge variant="secondary">Primary</Badge>}
                                    </div>
                                ))}
                            </div>
                        </SectionBody>
                    </TabsContent>

                    <TabsContent value="drive">
                        <SectionBody
                            section={drive}
                            family="drive"
                            itemCount={driveCount}
                            emptyLabel="This account's Drive is empty."
                            requestedScopes={requestedScopes}
                            onReconnect={onReconnect}
                            onRetry={onRetry}
                        >
                            <div className="space-y-4">
                                {drive.root_folders.length > 0 && (
                                    <div className="space-y-2">
                                        <p className="text-sm text-muted-foreground">
                                            Top-level folders
                                        </p>
                                        <div className="flex flex-wrap gap-2">
                                            {drive.root_folders.map((folder) => (
                                                <Badge
                                                    key={folder.id}
                                                    variant="outline"
                                                    className="gap-1.5 py-1"
                                                >
                                                    <Folder className="h-3 w-3" />
                                                    {folder.name}
                                                </Badge>
                                            ))}
                                        </div>
                                    </div>
                                )}
                                {drive.recent_files.length > 0 && (
                                    <div className="space-y-2">
                                        <p className="text-sm text-muted-foreground">Recent files</p>
                                        <div className="space-y-2">
                                            {drive.recent_files.map((file) => (
                                                <ResourceRow
                                                    key={file.id}
                                                    icon={<FileText className="h-4 w-4" />}
                                                    name={file.name}
                                                    meta={
                                                        formatTimestamp(file.modified_time)
                                                            ? `Modified ${formatTimestamp(file.modified_time)}`
                                                            : file.mime_type
                                                    }
                                                    href={file.web_url}
                                                />
                                            ))}
                                        </div>
                                    </div>
                                )}
                            </div>
                        </SectionBody>
                    </TabsContent>

                    <TabsContent value="gmail">
                        <SectionBody
                            section={gmail}
                            family="gmail"
                            itemCount={gmail.labels.length + gmail.recent.length}
                            emptyLabel="This mailbox has no labels or recent messages."
                            requestedScopes={requestedScopes}
                            onReconnect={onReconnect}
                            onRetry={onRetry}
                        >
                            <div className="space-y-4">
                                {gmail.labels.length > 0 && (
                                    <div className="space-y-2">
                                        <p className="text-sm text-muted-foreground">Labels</p>
                                        <div className="flex flex-wrap gap-1.5">
                                            {gmail.labels.map((label) => (
                                                <Badge
                                                    key={label.id}
                                                    variant={label.type === "user" ? "secondary" : "outline"}
                                                >
                                                    {label.name}
                                                </Badge>
                                            ))}
                                        </div>
                                    </div>
                                )}
                                {gmail.recent.length > 0 && (
                                    <div className="space-y-2">
                                        <p className="text-sm text-muted-foreground">
                                            Recent inbox messages
                                        </p>
                                        <div className="space-y-2">
                                            {gmail.recent.map((message) => (
                                                <div
                                                    key={message.id}
                                                    className="rounded-lg border border-border p-3"
                                                >
                                                    <div className="flex items-start gap-3">
                                                        <Mail className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                                                        <div className="min-w-0 space-y-1">
                                                            <p className="truncate font-medium">
                                                                {message.subject || "(no subject)"}
                                                            </p>
                                                            <p className="truncate text-xs text-muted-foreground">
                                                                {[message.from_address, message.date]
                                                                    .filter(Boolean)
                                                                    .join(" · ")}
                                                            </p>
                                                            {message.snippet && (
                                                                <p className="line-clamp-2 text-sm text-muted-foreground">
                                                                    {message.snippet}
                                                                </p>
                                                            )}
                                                        </div>
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}
                            </div>
                        </SectionBody>
                    </TabsContent>

                    <TabsContent value="docs">
                        <SectionBody
                            section={docs}
                            family="docs"
                            itemCount={docs.items.length}
                            emptyLabel="This account has no documents."
                            requestedScopes={requestedScopes}
                            onReconnect={onReconnect}
                            onRetry={onRetry}
                        >
                            <div className="space-y-2">
                                {docs.items.map((item) => (
                                    <ResourceRow
                                        key={item.id}
                                        icon={<FileText className="h-4 w-4" />}
                                        name={item.name}
                                        meta={
                                            formatTimestamp(item.modified_time)
                                                ? `Modified ${formatTimestamp(item.modified_time)}`
                                                : null
                                        }
                                        href={item.web_url}
                                    />
                                ))}
                            </div>
                        </SectionBody>
                    </TabsContent>
                </Tabs>
            </CardContent>
        </Card>
    );
}

export default WorkspaceResources;
