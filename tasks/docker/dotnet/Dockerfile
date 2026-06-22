# kirobench .NET verification image — SDK + warmed NuGet cache (xUnit).
# Microsoft's own container registry (mcr.microsoft.com) has no rate limits.
FROM mcr.microsoft.com/dotnet/sdk:8.0

ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    NUGET_PACKAGES=/opt/nuget

# Warm the NuGet cache so `dotnet test` works without network at run time.
WORKDIR /warm
COPY warm.csproj ./warm.csproj
RUN dotnet restore warm.csproj

WORKDIR /work
