FROM ghcr.io/dask/dask:latest

USER root
WORKDIR /home/mambauser/app
RUN chown mambauser:mambauser /home/mambauser/app

USER mambauser

COPY --chown=mambauser environment.yml .
RUN mamba env update -n base -f environment.yml && \
    mamba clean --all -y

# Pre-download cartopy data into lens2 environment
RUN micromamba run -n lens2 python -c "\
import cartopy.io.shapereader as shpreader; \
shpreader.natural_earth(resolution='110m', category='physical', name='coastline'); \
print('✓ Cartopy coastline data downloaded')"

COPY --chown=mambauser src/cesm-2-dashboard/ .

# Set environment variables (ENV_NAME will be set by Helm deployment)
ENV PYTHONUNBUFFERED=1
ENV PANEL_AUTORELOAD=false
ENV HDF5_USE_FILE_LOCKING=FALSE

EXPOSE 5006

# Simple CMD - environment auto-activates via ENV_NAME from deployment
CMD ["panel", "serve", "app.py", \
     "--address", "0.0.0.0", \
     "--port", "5006", \
     "--allow-websocket-origin=*", \
     "--num-procs", "1"]