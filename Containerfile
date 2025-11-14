FROM docker.io/mambaorg/micromamba:latest

USER root

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update

WORKDIR /home/mambauser/app

RUN chown mambauser:mambauser /home/mambauser/app

USER mambauser

# Copy environment.yml
COPY --chown=mambauser environment.yml .

# Install packages directly in base environment (no need for separate env)
RUN micromamba install -y -n base -c conda-forge --file environment.yml && \
    micromamba clean --all --yes

# Pre-download cartopy data
RUN python -c "\
import cartopy.io.shapereader as shpreader; \
shpreader.natural_earth(resolution='110m', category='physical', name='coastline'); \
print('✓ Cartopy data downloaded')"

# Copy application code
COPY --chown=mambauser src/cesm-2-dashboard/ .

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PANEL_AUTORELOAD=false
ENV HDF5_USE_FILE_LOCKING=FALSE

EXPOSE 5006

# Simpler CMD without micromamba run wrapper
CMD ["panel", "serve", "app.py", \
     "--address", "0.0.0.0", \
     "--port", "5006", \
     "--allow-websocket-origin=*", \
     "--num-procs", "1"]