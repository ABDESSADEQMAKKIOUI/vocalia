"use client";

import { ShieldAlert } from 'lucide-react';
import { type ReactNode, useEffect, useRef, useState } from 'react';

import { getAuthUserApiV1UserAuthUserGet } from '@/client/sdk.gen';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useAuth } from '@/lib/auth';

type AccessState = 'checking' | 'allowed' | 'denied';

/**
 * Gate the platform admin pages behind the superuser flag.
 *
 * The backend already rejects every /platform-admin route with a 403 for
 * regular users; this only avoids rendering a screen full of failed requests.
 */
export function SuperuserGuard({ children }: { children: ReactNode }) {
    const { user, getAccessToken, redirectToLogin, loading: authLoading } = useAuth();
    const [access, setAccess] = useState<AccessState>('checking');
    const hasChecked = useRef(false);

    useEffect(() => {
        if (!authLoading && !user) {
            redirectToLogin();
        }
    }, [authLoading, user, redirectToLogin]);

    useEffect(() => {
        if (authLoading || !user || hasChecked.current) return;
        hasChecked.current = true;

        const check = async () => {
            try {
                const accessToken = await getAccessToken();
                const response = await getAuthUserApiV1UserAuthUserGet({
                    headers: {
                        Authorization: `Bearer ${accessToken}`,
                    },
                });
                setAccess(response.data?.is_superuser ? 'allowed' : 'denied');
            } catch {
                setAccess('denied');
            }
        };
        check();
    }, [authLoading, user, getAccessToken]);

    if (authLoading || !user || access === 'checking') {
        return (
            <div className="container mx-auto px-4 py-8 space-y-4">
                <Skeleton className="h-10 w-72" />
                <Skeleton className="h-28 w-full" />
                <Skeleton className="h-64 w-full" />
            </div>
        );
    }

    if (access === 'denied') {
        return (
            <div className="container mx-auto px-4 py-8">
                <Card className="max-w-xl">
                    <CardHeader>
                        <CardTitle className="flex items-center gap-2">
                            <ShieldAlert className="h-5 w-5 text-destructive" />
                            Accès refusé
                        </CardTitle>
                        <CardDescription>
                            Cette console est réservée aux administrateurs de la
                            plateforme.
                        </CardDescription>
                    </CardHeader>
                    <CardContent>
                        <p className="text-sm text-muted-foreground">
                            Si vous pensez qu&apos;il s&apos;agit d&apos;une erreur,
                            demandez à un administrateur de vous accorder le rôle
                            superutilisateur.
                        </p>
                    </CardContent>
                </Card>
            </div>
        );
    }

    return <>{children}</>;
}
